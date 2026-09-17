"""JD-match scorer (PRD Epic B).

Two independent signals, deliberately kept separate in the output so the user
can see *why* a score is what it is:

* **Keyword coverage** - which terms the JD actually leans on, and whether the
  résumé uses them. Weighted by which JD section the term appeared in, because a
  term in "Requirements" is not worth the same as one in "About us". This is the
  layer that predicts a literal ATS keyword filter.
* **Semantic similarity** - embedding similarity between JD requirement
  sentences and résumé lines. This is the layer that catches "you clearly do
  this, you just called it something else".

The composite is a weighted blend. Everything here is deterministic apart from
the embedding model itself; no LLM call is involved.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from app.services import embeddings

LEXICON_PATH = Path(__file__).resolve().parent.parent / "data" / "skill_lexicon.json"

KEYWORD_WEIGHT = 0.55
SEMANTIC_WEIGHT = 0.45

# Section label -> how much a term found there counts.
SECTION_WEIGHTS: list[tuple[float, tuple[str, ...]]] = [
    (1.0, ("requirement", "qualification", "must have", "must-have", "what you'll need",
           "what you will need", "who you are", "skills and experience",
           "essential", "we're looking for", "we are looking for")),
    (0.9, ("responsibilit", "what you'll do", "what you will do", "the role",
           "about the role", "your impact", "day to day", "day-to-day",
           "key duties", "the job")),
    (0.4, ("nice to have", "nice-to-have", "bonus", "preferred", "desirable",
           "good to have", "plus points", "advantage")),
    (0.05, ("about us", "about the company", "who we are", "benefits", "perks",
            "what we offer", "our culture", "equal opportunit", "diversity",
            "how to apply", "compensation", "salary", "location")),
]
DEFAULT_SECTION_WEIGHT = 0.7
TITLE_WEIGHT = 1.3

# Calibration: raw cosine ranges differ hugely between a real sentence
# transformer and the lexical fallback, so each gets its own window mapped onto
# 0-100. Without this, fallback scores would look uniformly terrible.
SEMANTIC_WINDOW = {True: (0.18, 0.72), False: (0.06, 0.42)}

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#./-]*")
_GENERIC_MIN_LEN = 3
_GENERIC_MAX_NGRAM = 3


@dataclass
class TermHit:
    term: str
    weight: float
    found: bool
    source: str  # lexicon | jd_ngram
    matched_as: str  # the surface form actually seen in the résumé, or ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class MatchReport:
    composite: float
    keyword_score: float
    semantic_score: float
    backend: str
    semantic_backend_is_model: bool
    hits: list[TermHit] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    seniority: dict = field(default_factory=dict)

    @property
    def found(self) -> list[TermHit]:
        return [h for h in self.hits if h.found]

    @property
    def missing(self) -> list[TermHit]:
        return [h for h in self.hits if not h.found]

    def top_missing(self, n: int = 12) -> list[str]:
        return [h.term for h in sorted(self.missing, key=lambda h: -h.weight)[:n]]

    def as_dict(self) -> dict:
        return {
            "composite": round(self.composite, 1),
            "keyword_score": round(self.keyword_score, 1),
            "semantic_score": round(self.semantic_score, 1),
            "backend": self.backend,
            "semantic_backend_is_model": self.semantic_backend_is_model,
            "sections": self.sections,
            "seniority": self.seniority,
            "matched_terms": [h.as_dict() for h in sorted(self.found, key=lambda h: -h.weight)],
            "missing_terms": [h.as_dict() for h in sorted(self.missing, key=lambda h: -h.weight)],
            "top_missing": self.top_missing(),
        }


# --------------------------------------------------------------------------
# Lexicon
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_lexicon() -> dict:
    with LEXICON_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    data["_stopwords"] = set(_BASE_STOPWORDS) | set(data.get("stopwords_extra", []))
    return data


_BASE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "for",
    "from", "has", "have", "how", "in", "into", "is", "it", "its", "of", "on",
    "or", "our", "over", "so", "such", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "to", "up", "was", "were", "what",
    "when", "where", "which", "who", "why", "with", "would", "you", "your", "also",
    "any", "all", "more", "most", "other", "some", "one", "two", "three",
}


@lru_cache(maxsize=512)
def _phrase_pattern(phrase: str) -> re.Pattern:
    """Whole-phrase, punctuation-tolerant matcher.

    Word-boundary `\\b` is unusable here because terms like `ci/cd` and `c#` end
    in non-word characters, so lookarounds on the alphanumeric class are used
    instead. Internal spaces match any run of space, hyphen, underscore or slash
    so `power-bi` and `power bi` are the same term.
    """
    parts = [re.escape(p) for p in phrase.split()]
    body = r"[\s\-_/]+".join(parts)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.I)


# --------------------------------------------------------------------------
# JD parsing
# --------------------------------------------------------------------------


@dataclass
class JDSection:
    heading: str
    text: str
    weight: float


def split_jd_sections(jd_text: str, title: str = "") -> list[JDSection]:
    lines = jd_text.splitlines()
    sections: list[JDSection] = []
    if title.strip():
        sections.append(JDSection(heading="__title__", text=title.strip(), weight=TITLE_WEIGHT))

    current_heading = "__preamble__"
    current_weight = DEFAULT_SECTION_WEIGHT
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append(
                JDSection(heading=current_heading, text=body, weight=current_weight)
            )

    for raw in lines:
        line = raw.strip()
        if _looks_like_heading(line):
            flush()
            buffer = []
            current_heading = line.strip(":").strip()
            current_weight = _weight_for_heading(current_heading)
        else:
            buffer.append(raw)
    flush()

    if not sections:
        sections = [JDSection("__all__", jd_text.strip(), DEFAULT_SECTION_WEIGHT)]
    return sections


def _looks_like_heading(line: str) -> bool:
    if not (2 <= len(line) <= 70):
        return False
    if line[:1] in {"•", "-", "*", "–", "—", "·", "▪"}:
        return False
    stripped = line.rstrip(":").strip()
    if not stripped or stripped.endswith((".", ",", ";")):
        return False
    if len(stripped.split()) > 8:
        return False
    # Either an explicit "Heading:" or an ALL-CAPS / Title-Case standalone line
    # whose text matches a known section label.
    if line.endswith(":"):
        return True
    if stripped.isupper():
        return True
    return _weight_for_heading(stripped) != DEFAULT_SECTION_WEIGHT


def _weight_for_heading(heading: str) -> float:
    low = heading.lower()
    for weight, needles in SECTION_WEIGHTS:
        if any(needle in low for needle in needles):
            return weight
    return DEFAULT_SECTION_WEIGHT


# --------------------------------------------------------------------------
# Term extraction
# --------------------------------------------------------------------------


def extract_jd_terms(sections: list[JDSection]) -> dict[str, tuple[float, str]]:
    """Return `{canonical_term: (weight, source)}`.

    Two passes. The lexicon pass reliably canonicalises the skills that matter
    for this persona. The n-gram pass is the safety net that catches whatever
    the lexicon has never heard of - domain language, a niche product name, an
    industry term - so the scorer is not blind outside its vocabulary.
    """
    lexicon = load_lexicon()
    aliases: dict[str, list[str]] = lexicon["aliases"]
    stopwords: set[str] = lexicon["_stopwords"]

    terms: dict[str, tuple[float, str]] = {}

    # --- pass 1: lexicon ---
    for canonical, variants in aliases.items():
        weight = 0.0
        for section in sections:
            surface_forms = [canonical, *variants]
            count = sum(
                len(_phrase_pattern(form).findall(section.text)) for form in surface_forms
            )
            if count:
                # Diminishing returns: mentioned five times is not five times as
                # important as mentioned once.
                weight += section.weight * (1.0 + 0.35 * (min(count, 4) - 1))
        if weight > 0:
            terms[canonical] = (weight, "lexicon")

    # --- pass 2: generic n-grams ---
    lexicon_surface = {
        form.lower()
        for canonical, variants in aliases.items()
        for form in (canonical, *variants)
    }
    # Words already accounted for by a lexicon term found in this JD. Without
    # this, "Power BI" also yields a bare "power" term that double-counts.
    claimed_tokens: set[str] = set()
    for canonical in terms:
        for form in (canonical, *aliases.get(canonical, [])):
            claimed_tokens.update(_TOKEN_RE.findall(form.lower()))

    ngram_weights: dict[str, float] = {}
    for section in sections:
        if section.weight < 0.4:
            continue  # boilerplate: not worth mining
        for ngram, count in _count_ngrams(section.text, stopwords, claimed_tokens).items():
            if ngram in lexicon_surface:
                continue
            ngram_weights[ngram] = ngram_weights.get(ngram, 0.0) + section.weight * min(count, 3)

    ngram_weights = _keep_maximal_phrases(ngram_weights)

    # Keep only terms with real signal, so the denominator isn't diluted by the
    # long tail of one-off words.
    for ngram, weight in ngram_weights.items():
        if weight >= 1.2 and ngram not in terms:
            terms[ngram] = (weight * 0.6, "jd_ngram")

    return terms


def _count_ngrams(
    text: str, stopwords: set[str], claimed_tokens: set[str]
) -> dict[str, int]:
    """Contiguous n-grams over the raw token sequence.

    Filtering tokens *before* forming n-grams is the obvious implementation and
    it is wrong: dropping "bi" from "Senior Power BI Developer" welds the
    survivors together into "senior power developer", a phrase that appears
    nowhere in the posting. So the sequence stays intact and the filtering is
    applied to whole n-grams instead.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    counts: dict[str, int] = {}
    for n in range(1, _GENERIC_MAX_NGRAM + 1):
        for i in range(len(tokens) - n + 1):
            gram_tokens = tokens[i : i + n]
            first, last = gram_tokens[0], gram_tokens[-1]
            # A phrase must not start or end on a stopword or a bare number -
            # "with power" and "power and" are not terms.
            if first in stopwords or last in stopwords:
                continue
            if first.isdigit() or last.isdigit():
                continue
            if n == 1:
                if len(first) < _GENERIC_MIN_LEN or first in claimed_tokens:
                    continue
            elif len(first) < 2 or len(last) < 2:
                continue
            gram = " ".join(gram_tokens)
            counts[gram] = counts.get(gram, 0) + 1
    return counts


def _keep_maximal_phrases(weights: dict[str, float]) -> dict[str, float]:
    """Drop a phrase that only ever appears inside a longer, equally common one.

    N-gram extraction naturally yields "semantic", "semantic model" and "own the
    semantic model" for the same text. Keeping all three triples the weight of
    one requirement and clutters the missing-terms list, so the shorter forms are
    folded into the longest phrase that subsumes them.
    """
    kept: dict[str, float] = {}
    by_length = sorted(weights, key=lambda g: -len(g))
    for gram in by_length:
        weight = weights[gram]
        subsumed = any(
            longer != gram
            and _contains_phrase(longer, gram)
            and weights[longer] >= weight * 0.9
            for longer in kept
        )
        if not subsumed:
            kept[gram] = weight
    return kept


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Whole-word containment, so 'model' is not found inside 'modelling'."""
    hay = haystack.split()
    ned = needle.split()
    if len(ned) > len(hay):
        return False
    return any(hay[i : i + len(ned)] == ned for i in range(len(hay) - len(ned) + 1))


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def score(
    *,
    resume_text: str,
    jd_text: str,
    jd_title: str = "",
    extra_evidence: str = "",
) -> MatchReport:
    """Score one résumé against one JD.

    `extra_evidence` is optional profile text (skills, project descriptions) that
    counts as coverage even when it is not on the résumé itself - that gap is
    exactly what the rewrite step exists to close, and flagging it as "you have
    this, it's just not on the page" is more useful than reporting it missing.
    """
    sections = split_jd_sections(jd_text, jd_title)
    terms = extract_jd_terms(sections)

    lexicon = load_lexicon()
    aliases: dict[str, list[str]] = lexicon["aliases"]

    hits: list[TermHit] = []
    for term, (weight, source) in terms.items():
        surface_forms = [term, *aliases.get(term, [])] if source == "lexicon" else [term]
        matched_as = ""
        for form in surface_forms:
            pattern = _phrase_pattern(form)
            if pattern.search(resume_text):
                matched_as = form
                break
        if not matched_as and extra_evidence:
            for form in surface_forms:
                if _phrase_pattern(form).search(extra_evidence):
                    matched_as = f"{form} (profile only - not on the résumé)"
                    break
        hits.append(
            TermHit(
                term=term,
                weight=round(weight, 3),
                found=bool(matched_as),
                source=source,
                matched_as=matched_as,
            )
        )

    total_weight = sum(h.weight for h in hits)
    found_weight = sum(h.weight for h in hits if h.found)
    keyword_score = (found_weight / total_weight * 100.0) if total_weight else 0.0

    semantic_score, backend_name, is_model = _semantic_score(sections, resume_text)

    composite = KEYWORD_WEIGHT * keyword_score + SEMANTIC_WEIGHT * semantic_score

    return MatchReport(
        composite=round(composite, 1),
        keyword_score=round(keyword_score, 1),
        semantic_score=round(semantic_score, 1),
        backend=backend_name,
        semantic_backend_is_model=is_model,
        hits=hits,
        sections=[
            {"heading": s.heading, "weight": s.weight, "chars": len(s.text)} for s in sections
        ],
        seniority=_seniority_signal(resume_text, jd_text),
    )


def _semantic_score(sections: list[JDSection], resume_text: str) -> tuple[float, str, bool]:
    jd_units: list[tuple[str, float]] = []
    for section in sections:
        if section.weight < 0.4 or section.heading == "__title__":
            continue
        for sentence in _split_units(section.text):
            if len(sentence.split()) >= 4:
                jd_units.append((sentence, section.weight))

    resume_units = [u for u in _split_units(resume_text) if len(u.split()) >= 3]

    backend = embeddings.get_backend()
    if not jd_units or not resume_units:
        return 0.0, backend.name, backend.semantic

    scores = embeddings.best_match_scores([u for u, _ in jd_units], resume_units)
    weights = [w for _, w in jd_units]
    weighted = sum(s * w for s, w in zip(scores, weights, strict=True))
    denom = sum(weights)
    mean_cosine = weighted / denom if denom else 0.0

    lo, hi = SEMANTIC_WINDOW[backend.semantic]
    normalised = (mean_cosine - lo) / (hi - lo)
    return max(0.0, min(1.0, normalised)) * 100.0, backend.name, backend.semantic


def _split_units(text: str) -> list[str]:
    """Split into bullets and sentences - the granularity a résumé is written at."""
    units: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("•-*–—·▪◦‣>").strip()
        if not cleaned:
            continue
        if len(cleaned.split()) <= 25:
            units.append(cleaned)
        else:
            units.extend(
                part.strip() for part in re.split(r"(?<=[.;])\s+", cleaned) if part.strip()
            )
    return units


def _seniority_signal(resume_text: str, jd_text: str) -> dict:
    terms = load_lexicon().get("seniority_terms", [])
    resume_low = resume_text.lower()
    jd_low = jd_text.lower()
    in_jd = [t for t in terms if t in jd_low]
    in_resume = [t for t in terms if t in resume_low]
    covered = [t for t in in_jd if t in in_resume]
    ratio = len(covered) / len(in_jd) if in_jd else 1.0
    return {
        "jd_signals": in_jd,
        "resume_signals": in_resume,
        "covered": covered,
        "coverage_ratio": round(ratio, 2),
        "note": (
            "The JD uses ownership/leadership language the résumé does not mirror."
            if ratio < 0.5 and in_jd
            else ""
        ),
    }
