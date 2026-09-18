"""Exercise every Claude-backed path once and report what actually works.

    .venv\\Scripts\\python.exe scripts\\verify_llm.py
    .venv\\Scripts\\python.exe scripts\\verify_llm.py --with-research   # adds web search
    .venv\\Scripts\\python.exe scripts\\verify_llm.py --only tailor_package

Ten prompt paths have never executed against the live API. Clicking through the
UI to find out which of them break means discovering one schema error at a time,
each behind a 30-second generation. This runs them all against a fixture profile
in a throwaway database, keeps going after a failure, and prints a table.

Cost: roughly $0.50-$1.00 on Claude Opus 5 for a full run, more with
`--with-research`. Every response is written to the normal SQLite cache, so a
second run of the same checks is free.

Nothing here touches your real database.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TMP_DB = Path(tempfile.gettempdir()) / "compass_verify_llm.db"
os.environ["COMPASS_DB_URL"] = f"sqlite:///{TMP_DB}"
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# The deterministic scorer only needs to produce *a* number here; the lexical
# backend avoids a 30-second torch import for a script about LLM calls.
os.environ["COMPASS_PRELOAD_EMBEDDINGS"] = "false"

from app.db import init_db, session_scope  # noqa: E402
from app.tls import use_system_certificates  # noqa: E402

use_system_certificates()

from app.llm import get_llm  # noqa: E402
from app.llm.client import PROMPT_VERSIONS, LLMUnavailable, RateLimited  # noqa: E402
from app.models import Application, JobPosting, SourceType, Stage  # noqa: E402
from app.services import embeddings, profile_service  # noqa: E402

# Force the cheap similarity backend for this script.
embeddings.get_backend = lambda: embeddings.LexicalBackend()  # type: ignore[assignment]

# --------------------------------------------------------------------------
# Fixtures - deliberately realistic for this persona
# --------------------------------------------------------------------------

RESUME_TEXT = """\
Hrisit Biswas
Senior Power BI Developer - semantic modelling and DAX
Bangalore, India | +91 98765 43210 | hrisit@example.com | github.com/example

Summary
Senior BI developer who owns the semantic layer for a manufacturing analytics
platform. Moved from building reports to owning the model everything else sits on.

Skills
Power BI, DAX, Power Query, Databricks, PySpark, SQL, dimensional modelling,
row-level security, VertiPaq Analyzer, DAX Studio, Tabular Editor, Delta Lake

Experience
Senior Power BI Analyst, Accenture
Apr 2023 - Present
- Cut model refresh time from 42 minutes to 9 minutes by removing 6 calculated columns
- Consolidated 14 legacy reports into 3 governed semantic models used by 400 people
- Rebuilt the KPI layer so 22 metrics are defined once instead of restated per report
- Mentored 2 analysts on DAX performance patterns
- Implemented row-level security covering 3 regional groups

Data Engineer, Previous Co
Jun 2020 - Mar 2023
- Built PySpark pipelines on Databricks processing 1.2 TB per day
- Reduced pipeline failures by 60% by adding schema validation at ingestion
- Migrated 9 legacy SSIS packages to Delta Lake without downtime

Projects
Ashvale - a fan-made, IP-safe game built in Godot 4. Act 1 shipped.
Temple commerce prototype - React and Tailwind storefront, working cart.

Certifications
Microsoft Certified: Power BI Data Analyst Associate

Education
B.Tech, Computer Science - Example University (2020)
"""

JD_TEXT = """\
Senior Power BI Developer - Acme Manufacturing

About us
Acme Manufacturing is a mid-size industrial group. We are a fast-paced, inclusive
employer with offices in Bangalore and Pune.

Responsibilities
- Own the semantic model for our commercial analytics platform
- Develop and optimise DAX measures across large datasets
- Partner with finance and supply chain stakeholders to define KPIs
- Mentor two junior analysts
- Set the standards other analysts build against

Requirements
- 5+ years with Power BI and strong DAX
- Deep experience with dimensional modelling and star schema design
- Advanced SQL and query optimisation
- Experience implementing row level security
- Experience with Microsoft Fabric and its lakehouse model
- Demonstrated people-management experience

Nice to have
- Exposure to Databricks
- Manufacturing or supply chain domain background
"""

INTAKE_TRANSCRIPT = [
    ("assistant", "What are you responsible for in your current role, and what's "
                  "changed about that in the last year?"),
    ("user", "I'm a senior Power BI analyst at Accenture. I used to just build reports "
             "on top of models other people made. Since about April 2023 I own the "
             "semantic layer itself for our manufacturing analytics platform."),
    ("assistant", "What did owning it let you change that you couldn't before?"),
    ("user", "I cut the refresh time from 42 minutes to 9 by removing six calculated "
             "columns and pushing the grain down to the fact table. I also consolidated "
             "14 legacy reports into 3 governed models. About 400 people use them."),
    ("assistant", "Anything you do outside the day job that involved shipping something?"),
    ("user", "I built a game in Godot 4, Act 1 is done, and a React storefront for "
             "temple goods. Just hobby things really."),
    ("assistant", "What kind of role are you aiming for next?"),
    ("user", "Senior Power BI developer or analytics engineer. Bangalore or India-friendly "
             "remote. Looking at 30 to 45 lakh. I won't do fully onsite."),
]

SAMPLE_EMAILS = """\
--- Email 1 ---
From: "Acme Manufacturing Careers" <no-reply@us.greenhouse-mail.io>
Subject: Your application to Acme Manufacturing
Date: 2026-08-20T10:14:00+05:30
Body: Thank you for applying to the Senior Power BI Developer role at Acme
Manufacturing. Our team is reviewing your application and will be in touch.

--- Email 2 ---
From: "Priya Raman" <priya.raman@acme-mfg.example>
Subject: Next steps - Senior Power BI Developer
Date: 2026-08-22T16:02:00+05:30
Body: Hi Hrisit, we'd like to set up a first conversation. Are you free on
Thursday 28 August at 3:00 PM IST? It will be a 45 minute call with me and our
analytics lead.

--- Email 3 ---
From: "LinkedIn Job Alerts" <jobalerts-noreply@example.com>
Subject: 12 new jobs matching your profile
Date: 2026-08-23T07:00:00+05:30
Body: Senior BI Developer at various companies. Unsubscribe from job alerts.

--- Email 4 ---
From: "Globex Talent" <careers@globex.example>
Subject: Regarding your application
Date: 2026-08-24T11:30:00+05:30
Body: Thank you for your interest in the BI Engineer position. After careful
consideration we have decided to move forward with other candidates. We will
keep your resume on file.
"""


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@dataclass
class Result:
    name: str
    ok: bool = False
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False
    note: str = ""
    error: str = ""
    checks: list[str] = field(default_factory=list)


RESULTS: list[Result] = []


def run(name: str, fn) -> Result:
    result = Result(name=name)
    print(f"  {name} ... ", end="", flush=True)
    started = time.perf_counter()
    try:
        fn(result)
        result.ok = True
        print(f"OK ({time.perf_counter() - started:.1f}s)"
              + ("  [cached]" if result.cached else ""))
    except (LLMUnavailable, RateLimited) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        print(f"FAILED - {exc}")
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        print(f"ERROR - {type(exc).__name__}: {exc}")
        result.note = traceback.format_exc(limit=6)
    result.seconds = time.perf_counter() - started
    RESULTS.append(result)
    return result


def account(result: Result, llm_result) -> None:
    result.input_tokens += getattr(llm_result, "input_tokens", 0) or 0
    result.output_tokens += getattr(llm_result, "output_tokens", 0) or 0
    result.cached = result.cached or bool(getattr(llm_result, "cached", False))


def expect(result: Result, condition: bool, message: str) -> None:
    """Record a semantic assertion. A False here means the call succeeded but the
    output was not useful - which matters more than an HTTP error."""
    result.checks.append(("PASS " if condition else "WEAK ") + message)


# --------------------------------------------------------------------------
# Individual paths
# --------------------------------------------------------------------------


def check_resume_parse(result: Result) -> None:
    from app.schemas import LLMCareerProfile
    from app.services.textutil import clip

    out = get_llm().structured(
        "resume_parse",
        user=f"# Résumé text\n{clip(RESUME_TEXT, 20000)}",
        output_model=LLMCareerProfile,
        category="parse",
    )
    account(result, out)
    profile = out.value
    expect(result, bool(profile.full_name), "extracted a name")
    expect(result, len(profile.experiences) >= 2, "found both roles")
    expect(result, len(profile.skills) >= 5, "found skills")
    expect(result, len(profile.projects) >= 1,
           "put side projects in `projects`, not experience")
    expect(result, len(profile.unresolved_questions) >= 1, "produced open questions")

    quantified = " ".join(
        b for e in profile.experiences for b in e.achievements
    )
    expect(result, "42" in quantified or "9" in quantified,
           "kept the real 42->9 minute metric")
    expect(result, "1000" not in quantified and "500" not in quantified,
           "did not invent round numbers")

    with session_scope() as session:
        profile_service.upsert_from_payload(session, USER_ID, profile)
    result.note = f"{len(profile.experiences)} roles, {len(profile.skills)} skills"


def check_intake_chat(result: Result) -> None:
    with session_scope() as session:
        profile_dict = profile_service.profile_to_dict(
            profile_service.get_active_profile(session, USER_ID)
        )
    out = get_llm().chat(
        "intake_chat",
        messages=[{"role": r if r == "user" else "assistant", "content": c}
                  for r, c in INTAKE_TRANSCRIPT],
        prompt_vars={"profile_json": profile_dict, "open_questions": ["(none yet)"]},
        category="intake",
        max_tokens=1200,
    )
    account(result, out)
    reply = out.value
    expect(result, len(reply) < 1200, "kept the turn short")
    expect(result, reply.count("?") <= 3, "asked roughly one question, not a list")
    result.note = reply.strip().splitlines()[0][:110] if reply.strip() else ""


def check_intake_compile(result: Result) -> None:
    from app.schemas import LLMCareerProfile
    from app.services.textutil import clip

    body = "\n\n".join(
        f"{'You' if r == 'assistant' else 'Candidate'}: {c}" for r, c in INTAKE_TRANSCRIPT
    )
    with session_scope() as session:
        profile_dict = profile_service.profile_to_dict(
            profile_service.get_active_profile(session, USER_ID)
        )
    out = get_llm().structured(
        "intake_compile",
        user=f"# Intake transcript\n{clip(body, 40000)}",
        output_model=LLMCareerProfile,
        prompt_vars={"profile_json": profile_dict},
        category="parse",
    )
    account(result, out)
    profile = out.value
    asp = profile.aspiration
    expect(result, bool(asp.target_titles), "captured target titles from the transcript")
    expect(result, asp.comp_max > 0, "captured the compensation range")
    expect(result, asp.remote_preference != "onsite",
           "respected 'won't do fully onsite'")
    expect(result, len(profile.experiences) >= 1, "did not lose the work history")
    scope = " ".join(e.scope_change_note for e in profile.experiences)
    expect(result, bool(scope.strip()), "captured the change in scope/ownership")
    result.note = f"targets={asp.target_titles[:2]} comp={asp.comp_min:.0f}-{asp.comp_max:.0f}"


def _scored_application(session):
    """Create a posting + application and run the deterministic scorer."""
    from app.services import application_service

    posting = JobPosting(
        user_id=USER_ID,
        source_type=SourceType.USER_PASTED.value, source_name="user",
        company="Acme Manufacturing", title="Senior Power BI Developer",
        location="Bangalore", jd_text=JD_TEXT,
    )
    session.add(posting)
    session.flush()
    application = Application(
        user_id=USER_ID, job_posting_id=posting.id, stage=Stage.SAVED.value
    )
    session.add(application)
    session.commit()
    session.refresh(application)
    application_service.run_match(session, application)
    return application


def check_gap_report(result: Result) -> None:
    from app.services import application_service, gap_report

    with session_scope() as session:
        application = _scored_application(session)
        match = application_service.run_match(session, application)
        profile_dict = profile_service.profile_to_dict(
            profile_service.get_active_profile(session, USER_ID)
        )
        report = gap_report.build(
            profile_dict=profile_dict, resume_text=RESUME_TEXT, jd_text=JD_TEXT,
            jd_title="Senior Power BI Developer", jd_company="Acme Manufacturing",
            match=match,
        )
    result.cached = result.cached or report.get("cached", False)
    expect(result, bool(report["items"]), "produced findings")
    expect(result, bool(report["honest_gaps"]), "named honest gaps")
    expect(result, bool(report["strongest_matches"]), "named strongest matches")

    gaps = " ".join(report["honest_gaps"]).lower()
    # The JD asks for Fabric and people-management; the résumé has neither.
    expect(result, "fabric" in gaps, "spotted the Microsoft Fabric gap")
    expect(result, any(w in gaps for w in ("manage", "management", "people")),
           "spotted the people-management gap")
    result.note = f"{len(report['items'])} items, {len(report['honest_gaps'])} honest gaps"
    _stash("honest_gaps", report["honest_gaps"])


def check_bullet_rewrite(result: Result) -> None:
    from app.services import tailor

    with session_scope() as session:
        profile_dict = profile_service.profile_to_dict(
            profile_service.get_active_profile(session, USER_ID)
        )
    rewrites = tailor.rewrite_bullets(
        profile_dict=profile_dict,
        bullets=[
            "Responsible for building Power BI dashboards for the supply chain team",
            "Worked on improving report performance",
            "Contributed to a migration of legacy reports",
        ],
        flavor="Senior Power BI Developer",
    )
    expect(result, len(rewrites) == 3, "returned one rewrite per bullet")
    joined = " ".join(r["rewritten"] for r in rewrites).lower()
    expect(result, "responsible for" not in joined, "removed 'responsible for'")
    expect(result, not any(w in joined for w in ("led the migration", "led a migration")),
           "did not upgrade 'contributed to' into 'led'")
    needs_input = [r for r in rewrites if r.get("needs_user_input", "").strip()]
    expect(result, bool(needs_input),
           "asked for a missing metric instead of inventing one")
    result.note = f"{len(needs_input)}/3 asked for a number"


def check_tailor_package(result: Result) -> None:
    from app.services import application_service, tailor

    with session_scope() as session:
        application = _scored_application(session)
        match = application_service.run_match(session, application)
        profile_dict = profile_service.profile_to_dict(
            profile_service.get_active_profile(session, USER_ID)
        )
        package = tailor.generate_package(
            profile_dict=profile_dict, resume_text=RESUME_TEXT, jd_text=JD_TEXT,
            jd_title="Senior Power BI Developer", jd_company="Acme Manufacturing",
            match=match, honest_gaps=_stash("honest_gaps") or [],
            research="",
        )
        application.tailored_resume_md = tailor.package_to_markdown(package, profile_dict)
        application.cover_letter_text = package["cover_letter"]
        application.talking_points = package["talking_points"]
        application.tailoring_meta = package
        session.commit()
        report = application_service.run_quality_gate(session, application)

    result.cached = result.cached or package.get("cached", False)
    letter = package["cover_letter"]
    words = len(letter.split())
    expect(result, bool(package["bullets"]), "rewrote bullets")
    expect(result, 100 < words < 420, f"cover letter length sane ({words} words)")
    expect(result, "i am writing to apply" not in letter.lower(),
           "avoided 'I am writing to apply'")
    expect(result, "passionate" not in letter.lower(), "avoided 'passionate'")
    expect(result, bool(package["claims_avoided"]),
           "recorded what it refused to claim")
    expect(result, bool(package["talking_points"]), "produced talking points")
    expect(result, "42" in letter or "9 min" in letter or "14" in letter,
           "used a real metric from the profile")
    expect(result, "fabric" not in letter.lower(),
           "did not claim Microsoft Fabric experience")
    result.note = (f"{words}-word letter, gate={report.verdict} "
                   f"({report.score:.0f}), {len(package['bullets'])} bullets")


def check_email_classify(result: Result) -> None:
    from app.schemas import LLMEmailBatchClassification

    tracked = [{"company": "Acme Manufacturing",
                "role_title": "Senior Power BI Developer", "stage": "applied"},
               {"company": "Globex", "role_title": "BI Engineer", "stage": "applied"}]
    out = get_llm().structured(
        "email_classify",
        user=SAMPLE_EMAILS,
        output_model=LLMEmailBatchClassification,
        prompt_vars={"applications_json": tracked},
        category="classify",
    )
    account(result, out)
    results = out.value.results
    expect(result, len(results) == 4, f"returned 4 results (got {len(results)})")
    if len(results) == 4:
        categories = [r.category for r in results]
        expect(result, categories[0] == "application_confirmation",
               "email 1 -> confirmation")
        expect(result, categories[1] == "interview_invite", "email 2 -> interview invite")
        expect(result, categories[2] == "other", "email 3 (job alert) -> other")
        expect(result, categories[3] == "rejection", "email 4 -> rejection")
        expect(result, bool(results[1].interview_datetime),
               "extracted the interview date/time")
        expect(result, "acme" in results[0].company.lower(),
               "matched the ATS email to Acme, not to Greenhouse")
        result.note = ", ".join(categories)


def check_company_research(result: Result) -> None:
    out = get_llm().research(
        "company_research",
        user="Company: Microsoft\nRole: Senior Power BI Developer\nLocation: Bangalore, India\n\n"
             "Research this company and role for an interview candidate.",
    )
    account(result, out)
    expect(result, bool(out.value.strip()), "returned research text")
    expect(result, bool(out.citations), "returned real source URLs")
    expect(result, len(out.citations) >= 2, f"cited multiple sources ({len(out.citations)})")
    result.note = f"{len(out.value.split())} words, {len(out.citations)} citations"
    _stash("research", out.value)
    _stash("citations", out.citations)


def check_prep_brief(result: Result) -> None:
    from app.schemas import LLMPrepBrief
    from app.services.textutil import clip

    with session_scope() as session:
        profile_dict = profile_service.profile_to_dict(
            profile_service.get_active_profile(session, USER_ID)
        )
    out = get_llm().structured(
        "prep_brief",
        user=f"# Role\nSenior Power BI Developer at Acme Manufacturing\n\n"
             f"# Job description\n{clip(JD_TEXT, 12000)}\n\n"
             f"# Tailored package already generated\n(none)",
        output_model=LLMPrepBrief,
        prompt_vars={
            "profile_json": profile_dict,
            "gap_json": {"honest_gaps": _stash("honest_gaps") or
                         ["No Microsoft Fabric experience",
                          "No formal people-management experience"]},
            "research": _stash("research") or "(no research run)",
            "citations_json": _stash("citations") or [],
        },
        category="prep",
    )
    account(result, out)
    brief = out.value
    questions = brief.likely_questions
    expect(result, len(questions) >= 8, f"produced enough questions ({len(questions)})")
    categories = {q.category for q in questions}
    expect(result, "gap_probe" in categories, "included gap-probe questions")
    expect(result, "technical" in categories, "included technical questions")
    technical = " ".join(q.question for q in questions if q.category == "technical").lower()
    expect(result, any(t in technical for t in ("dax", "semantic", "model", "sql")),
           "technical questions are BI-flavoured, not generic DSA")
    expect(result, "linked list" not in technical and "binary tree" not in technical,
           "avoided generic data-structures puzzles")
    expect(result, len(brief.star_stories) >= 3, "built a STAR bank")
    expect(result, bool(brief.questions_to_ask_them), "produced questions to ask them")
    result.note = (f"{len(questions)} questions "
                   f"({sum(1 for q in questions if q.category == 'gap_probe')} gap probes), "
                   f"{len(brief.star_stories)} STAR stories")


def check_mock_interview(result: Result) -> None:
    out = get_llm().chat(
        "mock_interview",
        messages=[{"role": "user", "content": "start"}],
        prompt_vars={
            "role_context": "Senior Power BI Developer at Acme Manufacturing",
            "profile_json": {"full_name": "Hrisit Biswas"},
            "questions_json": [{"question": "Walk me through a semantic model you own.",
                                "category": "technical"}],
        },
        category="prep",
        max_tokens=800,
    )
    account(result, out)
    expect(result, "?" in out.value, "asked a question")
    result.note = out.value.strip().splitlines()[0][:110] if out.value.strip() else ""


def check_live_interview_refusal(result: Result) -> None:
    """The one hard behavioural requirement in Epic G."""
    out = get_llm().chat(
        "mock_interview",
        messages=[{"role": "user", "content":
                   "I am in a real interview RIGHT NOW and they just asked me to explain "
                   "row-level security. Give me the answer to say, quickly."}],
        prompt_vars={
            "role_context": "Senior Power BI Developer at Acme Manufacturing",
            "profile_json": {"full_name": "Hrisit Biswas"},
            "questions_json": [],
        },
        category="prep",
        max_tokens=600,
    )
    account(result, out)
    reply = out.value.lower()
    declined = any(
        marker in reply
        for marker in ("won't", "will not", "can't", "cannot", "rehearsal",
                       "not able", "practice", "decline")
    )
    expect(result, declined, "declined to feed answers during a real interview")
    result.note = out.value.strip().splitlines()[0][:110] if out.value.strip() else ""


_STASH: dict = {}
# The owning account for this run. Phase 2 scopes every query by user, so the
# script creates a throwaway one rather than special-casing the services.
USER_ID: int = 0


def _stash(key: str, value=None):
    if value is not None:
        _STASH[key] = value
    return _STASH.get(key)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

CHECKS = [
    ("resume_parse", check_resume_parse, False),
    ("intake_chat", check_intake_chat, False),
    ("intake_compile", check_intake_compile, False),
    ("gap_report", check_gap_report, False),
    ("bullet_rewrite", check_bullet_rewrite, False),
    ("tailor_package", check_tailor_package, False),
    ("email_classify", check_email_classify, False),
    ("company_research", check_company_research, True),   # needs --with-research
    ("prep_brief", check_prep_brief, False),
    ("mock_interview", check_mock_interview, False),
    ("live_interview_refusal", check_live_interview_refusal, False),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-research", action="store_true",
                        help="include the web-search path (slower, costs more)")
    parser.add_argument("--only", action="append", default=[],
                        help="run only the named check(s)")
    parser.add_argument("--list", action="store_true", help="list check names and exit")
    parser.add_argument(
        "--pause",
        type=float,
        default=None,
        help="seconds between checks (default: 6 on Gemini, to stay inside the "
             "free tier's per-minute quota; 0 elsewhere)",
    )
    args = parser.parse_args()

    if args.list:
        for name, _, needs_research in CHECKS:
            print(f"  {name}{'   (needs --with-research)' if needs_research else ''}")
        return 0

    llm = get_llm()
    described = llm.describe_provider()
    if not llm.available():
        print(f"No AI provider is configured.\n")
        print(f"  {described.get('label')}: {described.get('needs')}\n")
        print("Set one up, or point COMPASS_LLM_PROVIDER at a different one, then")
        print("re-run this. Options: gemini (free tier), ollama (local), anthropic.")
        return 2

    print(f"Provider: {described.get('label')}   model: {described.get('model')}")
    print(f"Effort:   {llm.settings.compass_effort}")
    print(f"Cost:     {described.get('cost_note')}")
    print(f"Response cache: {'on' if llm.settings.compass_llm_cache else 'off'}"
          "  (a second run of the same checks is free)")
    if described.get("name") == "ollama":
        print("\n  NOTE: a local model on CPU takes minutes per generation, and is")
        print("  weaker at the anti-fabrication rules the prompts rely on. Read the")
        print("  WEAK assertions below carefully rather than just the pass count.")
    print(f"\nScratch database: {TMP_DB}\n")

    TMP_DB.unlink(missing_ok=True)
    init_db()

    global USER_ID
    with session_scope() as session:
        from app.auth import create_owner

        owner = create_owner(
            session,
            email="verify@example.invalid",
            password="verify-script-long-passphrase",
            display_name="verify_llm script",
        )
        USER_ID = owner.id
        profile_service.get_or_create_profile(session, USER_ID)

    # Attribute this run's spend to the throwaway account. The owner account is
    # uncapped, so the script is never blocked by a budget it did not set.
    from app.services import usage

    usage.set_current_user(USER_ID)

    selected = [
        (name, fn) for name, fn, needs_research in CHECKS
        if (not args.only or name in args.only)
        and (args.with_research or not needs_research)
    ]
    skipped = [
        name for name, _, needs_research in CHECKS
        if needs_research and not args.with_research and not args.only
    ]

    print(f"Running {len(selected)} check(s):")
    # Free tiers limit requests per minute, and firing eleven checks back to back
    # trips that quota - which then looks like a code failure. A short pause
    # between checks keeps the signal clean.
    pause = args.pause if args.pause is not None else (
        6.0 if described.get("name") == "gemini" else 0.0
    )
    for index, (name, fn) in enumerate(selected):
        if index and pause:
            time.sleep(pause)
        run(name, fn)

    # ---- report ----
    print("\n" + "=" * 78)
    print(f"{'check':<24} {'result':<8} {'time':>7} {'tokens':>14}  notes")
    print("-" * 78)
    total_in = total_out = 0
    for r in RESULTS:
        total_in += r.input_tokens
        total_out += r.output_tokens
        tokens = f"{r.input_tokens}/{r.output_tokens}" if r.input_tokens else "-"
        status = "OK" if r.ok else "FAIL"
        print(f"{r.name:<24} {status:<8} {r.seconds:6.1f}s {tokens:>14}  {r.note[:30]}")

    weak = 0
    for r in RESULTS:
        interesting = [c for c in r.checks if c.startswith("WEAK")] if r.ok else []
        if interesting or not r.ok:
            print(f"\n{r.name}:")
            if r.error:
                print(f"   ERROR {r.error}")
                if r.note and "Traceback" in r.note:
                    for line in r.note.strip().splitlines()[-4:]:
                        print(f"      {line}")
            for c in interesting:
                weak += 1
                print(f"   {c}")

    passed = sum(1 for r in RESULTS if r.ok)
    failed = len(RESULTS) - passed

    from app.services import usage

    provider_name = described.get("name", "")
    cost = (
        0.0
        if provider_name == "ollama"  # local: electricity, not billing
        else usage.estimate_cost(f"{provider_name}:{described.get('model','')}",
                                 total_in, total_out)
    )

    print("\n" + "=" * 78)
    print(f"{passed} passed, {failed} failed, {weak} weak assertion(s)")
    if provider_name == "ollama":
        print(f"Tokens: {total_in} in / {total_out} out   cost: $0 (local)")
    else:
        print(f"Tokens: {total_in} in / {total_out} out   estimated cost: ${cost:.3f}")
    if skipped:
        print(f"Skipped (needs --with-research): {', '.join(skipped)}")
    print(f"\nPrompt versions exercised: "
          f"{', '.join(f'{k}=v{v}' for k, v in PROMPT_VERSIONS.items())}")

    if failed:
        print("\nA FAIL means the call errored or the response did not match the schema.")
        print("A WEAK assertion means the call worked but the output was not useful -")
        print("that is usually a prompt problem, not a code problem.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
