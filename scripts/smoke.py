"""End-to-end smoke walk: boot Compass in-process and exercise every route.

    python scripts/smoke.py

Runs against a throwaway database with no credentials configured, so it covers
the half of Compass that must work without an API key or a Google connection -
and it asserts that the LLM-backed routes degrade into a friendly banner rather
than a 500. Finishes by printing the scores the deterministic pipeline actually
produced, which is the quickest way to see whether a scoring change did what you
expected.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TMP_DB = Path(tempfile.gettempdir()) / "compass_smoke.db"
TMP_DB.unlink(missing_ok=True)
os.environ["COMPASS_DB_URL"] = f"sqlite:///{TMP_DB}"
os.environ["COMPASS_SECRET_KEY"] = "smoke-secret"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

JD = """\
Senior Power BI Developer

About us
We are a fast-paced and inclusive employer with great benefits in Bangalore.

Responsibilities
- Own the semantic model for our commercial analytics platform
- Develop and optimise DAX measures across large datasets
- Partner with stakeholders to define KPIs
- Mentor junior analysts

Requirements
- 5+ years with Power BI and strong DAX
- Deep experience with dimensional modelling and star schema design
- Advanced SQL and query optimisation
- Experience with row level security

Nice to have
- Exposure to Databricks
"""

failures: list[str] = []


def check(label: str, response, expect: int = 200):
    ok = response.status_code == expect
    print(f"{'PASS' if ok else 'FAIL'}  {response.status_code}  {label}")
    if not ok:
        failures.append(f"{label} -> {response.status_code} (expected {expect})")
        print("      ", response.text[:600].replace("\n", " "))
    return response


with TestClient(app, follow_redirects=False) as client:
    print("=== auth wall (Phase 2) ===")
    for path in ("/", "/profile", "/resumes", "/discover", "/applications",
                 "/tracker", "/settings"):
        check(f"GET {path} (signed out)", client.get(path), 303)
    check("GET /login -> /setup on first run", client.get("/login"), 303)
    check("GET /setup", client.get("/setup"))
    check("POST /setup (create owner)", client.post("/setup", data={
        "email": "smoke@example.com", "display_name": "Smoke Owner",
        "password": "smoke-long-enough-passphrase",
        "confirm": "smoke-long-enough-passphrase",
    }), 303)

    print("\n=== GET pages (signed in, empty database) ===")
    for path in ("/", "/profile", "/resumes", "/discover", "/applications",
                 "/tracker", "/settings"):
        check(f"GET {path}", client.get(path))

    print("\n=== Epic A: profile forms ===")
    check("POST /profile/basics", client.post("/profile/basics", data={
        "full_name": "Hrisit Biswas",
        "email": "hrisit@example.com",
        "phone": "+91 98765 43210",
        "location": "Bangalore, India",
        "headline": "Senior Power BI Developer - semantic modelling and DAX",
        "summary": "Owns the semantic layer for a manufacturing analytics platform.",
        "links": "github.com/example\nexample.dev/portfolio",
        "intake_notes": "",
    }), 303)

    check("POST /profile/skills", client.post("/profile/skills", data={
        "skills": "Power BI | platform | expert\nDAX | hard | strong\n"
                  "Dimensional modelling | hard | strong\nSQL | hard | strong\n"
                  "PySpark | hard | working\nDatabricks | platform | working\n"
                  "Row level security | hard | strong\nDAX Studio | tool | working",
    }), 303)

    check("POST /profile/experience", client.post("/profile/experience", data={
        "title": "Senior Power BI Analyst",
        "company": "Accenture",
        "location": "Bangalore",
        "start_date": "2023-04",
        "end_date": "",
        "is_current": "true",
        "achievements":
            "Cut model refresh from 42 minutes to 9 minutes by removing 6 calculated columns\n"
            "Consolidated 14 legacy reports into 3 governed semantic models used by 400 people\n"
            "Mentored 2 analysts on DAX performance patterns",
        "responsibilities":
            "Own the semantic layer and the KPI definitions\n"
            "Implement row level security across regional groups",
        "scope_change_note":
            "Took over the semantic layer end to end; previously only built reports on it",
    }), 303)

    check("POST /profile/project", client.post("/profile/project", data={
        "name": "Ashvale",
        "description": "A fan-made, IP-safe game built in Godot 4.",
        "highlight": "Shipped Act 1 with its own design document and a save system.",
        "tech": "Godot 4, GDScript",
        "link": "github.com/example/ashvale",
        "status": "shipped",
    }), 303)

    check("POST /profile/education", client.post("/profile/education", data={
        "degree": "B.Tech", "field": "Computer Science",
        "institution": "Example University", "end_year": "2020",
    }), 303)

    check("POST /profile/aspirations", client.post("/profile/aspirations", data={
        "target_titles": "Senior Power BI Developer\nAnalytics Engineer",
        "target_industries": "Manufacturing\nRetail",
        "target_companies": "",
        "locations": "Bangalore\nRemote (India)",
        "remote_preference": "hybrid",
        "comp_min": "3000000", "comp_max": "4500000", "comp_currency": "INR",
        "non_negotiables": "No 100% onsite",
    }), 303)

    check("GET /profile (populated)", client.get("/profile"))

    print("\n=== Epic B: generate a variant + ATS check ===")
    check("POST /resumes/generate", client.post(
        "/resumes/generate", data={"label": "Generated baseline", "flavor": "base"}
    ), 303)
    check("GET /resumes", client.get("/resumes"))
    check("GET /resumes/1", client.get("/resumes/1"))
    check("POST /resumes/1/recheck", client.post("/resumes/1/recheck"))
    check("GET /resumes/1/export.docx", client.get("/resumes/1/export.docx"))
    check("GET /resumes/1/export.pdf", client.get("/resumes/1/export.pdf"))

    print("\n=== Epic D: application + deterministic scoring ===")
    check("POST /applications", client.post("/applications", data={
        "company": "Acme Manufacturing",
        "title": "Senior Power BI Developer",
        "location": "Bangalore",
        "url": "https://example.com/jobs/1",
        "jd_text": JD,
    }), 303)
    check("GET /applications", client.get("/applications"))
    check("GET /applications/1", client.get("/applications/1"))
    check("POST /applications/1/score", client.post("/applications/1/score"))
    check("POST /applications/1/gate", client.post("/applications/1/gate"))
    check("POST /applications/1/note", client.post(
        "/applications/1/note", data={"summary": "Recruiter is a college contact"}
    ), 303)
    check("POST /applications/1/stage", client.post(
        "/applications/1/stage", data={"stage": "applied"}
    ), 303)

    print("\n=== Epic C: discovery (no network) ===")
    # A board token that cannot resolve, to prove the failure path stays a
    # banner rather than a 500. The real adapters are covered by
    # tests/test_discovery.py against recorded payloads.
    check("POST /discover/sources (unreachable board)", client.post(
        "/discover/sources",
        data={"ats_name": "greenhouse", "board_token": "compass-smoke-no-such-board"},
    ), 303)
    check("POST /discover/refresh (no sources)", client.post("/discover/refresh"), 303)
    check("POST /discover/rank (empty corpus)", client.post("/discover/rank"), 303)
    check("GET /discover (filters)", client.get(
        "/discover?sort=current&remote=yes&show=all&q=engineer"))
    check("GET /discover (dismissed view)", client.get("/discover?show=dismissed"))
    check("GET /discover/999 adopt (not mine)",
          client.post("/discover/999/adopt"), 404)

    print("\n=== Epic E: tracker ===")
    check("GET /tracker", client.get("/tracker"))
    check("POST /tracker/1/stage", client.post("/tracker/1/stage", data={"stage": "screen"}))

    print("\n=== LLM routes with no credentials -> banner, not a 500 ===")
    for label, path, data in (
        ("gap report", "/applications/1/gaps", None),
        ("intake turn", "/profile/intake/message", {"message": "hello"}),
        ("bullet rewrite", "/resumes/rewrite",
         {"bullets": "Responsible for building dashboards", "flavor": "general"}),
    ):
        check(f"POST {path} ({label})",
              client.post(path, data=data, headers={"hx-request": "true"}))

    print("\n=== Epic G page renders with no brief yet ===")
    check("GET /prep/1", client.get("/prep/1"))

    print("\n=== Export before generating -> redirect, not a crash ===")
    check("export resume (no package)",
          client.get("/applications/1/export/resume.docx"), 303)
    check("export cover letter (no package)",
          client.get("/applications/1/export/cover-letter.pdf"), 303)

    print("\n=== A hand edit re-runs the gate ===")
    check("POST /applications/1/edit", client.post("/applications/1/edit", data={
        "tailored_resume_md":
            "# Hrisit Biswas\n## Summary\nHand-edited.\n"
            "## Experience\n### Senior Power BI Analyst, Accenture\n"
            "Apr 2023 - Present\n- Cut refresh from 42 to 9 minutes\n",
        "cover_letter_text":
            "Acme Manufacturing's move onto a governed semantic model is exactly the "
            "migration I ran last year: 42 minutes down to 9.",
        "notes": "Referred by a college contact",
        "resume_variant_id": "1",
    }), 303)
    check("export resume (after edit)", client.get("/applications/1/export/resume.docx"))
    check("export cover letter (after edit)",
          client.get("/applications/1/export/cover-letter.pdf"))

    print("\n=== 'Ready' is blocked while the gate says templated ===")
    check("POST ready (no reason)",
          client.post("/applications/1/ready", data={"override_reason": ""}), 303)
    check("POST ready (with reason)", client.post(
        "/applications/1/ready", data={"override_reason": "Sister company, same role"}
    ), 303)

    print("\n=== Sync with no Google connection ===")
    check("POST /tracker/sync/gmail",
          client.post("/tracker/sync/gmail", headers={"hx-request": "true"}))
    check("POST /tracker/sync/calendar",
          client.post("/tracker/sync/calendar", headers={"hx-request": "true"}))

    print("\n=== 404s and bad input ===")
    # Both are 404 rather than a friendly redirect: since Phase 2 these ids go
    # through an ownership check, and "not yours" must be indistinguishable from
    # "does not exist" so the URL space cannot be probed.
    check("GET /applications/999", client.get("/applications/999"), 404)
    check("GET /resumes/999", client.get("/resumes/999"), 404)
    check("POST /applications (JD too short)",
          client.post("/applications", data={"company": "X", "jd_text": "too short"}), 303)
    check("GET /resumes/1/export.txt (bad format)", client.get("/resumes/1/export.txt"), 303)

    print("\n=== Google not configured -> explains itself ===")
    check("GET /google/connect", client.get("/google/connect"), 303)

    print("\n=== invites (admin only) ===")
    check("POST /settings/invite", client.post("/settings/invite", data={
        "email": "friend@example.com", "display_name": "Friend",
        "monthly_budget_usd": "5.00",
    }), 303)

    print("\n=== Dashboard with data ===")
    check("GET /", client.get("/"))

    print("\n=== sign out ===")
    check("POST /logout", client.post("/logout"), 303)
    check("GET / after logout", client.get("/"), 303)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for failure in failures:
        print("  -", failure)
    raise SystemExit(1)
print("Smoke test passed: every route responded as expected.\n")

from app.db import session_scope  # noqa: E402
from app.models import Application, ResumeVariant  # noqa: E402

with session_scope() as session:
    variant = session.get(ResumeVariant, 1)
    application = session.get(Application, 1)

    print(f"ATS score (generated variant): {variant.ats_score:.0f}/100 "
          f"({variant.ats_report['grade']}) - "
          f"{len(variant.ats_report['findings'])} finding(s)")
    for finding in variant.ats_report["findings"]:
        print(f"   - {finding['severity']}: {finding['title']}")

    match = application.match_report
    print(f"\nJD match: composite {match['composite']} "
          f"(keyword {match['keyword_score']}, semantic {match['semantic_score']}) "
          f"via {match['backend']}")
    print("   matched:", ", ".join(h["term"] for h in match["matched_terms"][:8]))
    print("   missing:", ", ".join(match["top_missing"][:8]))

    print(f"\nQuality gate: {application.quality_report['score']:.0f} "
          f"-> {application.quality_verdict}")
    for signal in application.quality_report["signals"]:
        print(f"   - {signal['verdict']:5} {signal['label']}: {signal['detail'][:90]}")

    print(f"\nStage: {application.stage}; timeline entries: {len(application.events)}")
