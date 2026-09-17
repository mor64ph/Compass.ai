"""Measure real horizontal overflow at mobile widths, and name the element causing it.

    .venv\\Scripts\\python.exe scripts\\check_responsive.py
    .venv\\Scripts\\python.exe scripts\\check_responsive.py --shots

Why this exists rather than a screenshot
----------------------------------------
`chrome --headless --window-size=375,812` does **not** set the layout viewport.
The page lays out at Chrome's floor (~512px) and the screenshot is then *cropped*
to the requested size, so text cut off at the right edge looks exactly like
overflow whether or not any exists. Conclusions drawn from those screenshots are
worthless - a mobile overflow bug has been "found" that way twice, on a different
project, and both were artifacts.

Playwright drives Chrome over CDP and calls `Emulation.setDeviceMetricsOverride`,
which sets the layout viewport for real. `window.innerWidth` here is the width
asked for.

What it measures
----------------
Per page per viewport: `documentElement.scrollWidth - clientWidth`. Zero is the
only passing answer. When it is not zero, every element whose box extends past
the viewport is listed with its tag, classes and measured right edge - because
"this page overflows by 84px" is not actionable and "TABLE.none right=412" is.

Requires playwright (dev-only, not in requirements.txt):
    pip install playwright
It uses the Chrome already installed, so no browser download is needed.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import tempfile

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Real layout widths, not device pixels. 320 is the narrowest phone still in use
# (iPhone SE 1st gen); 360 is the most common Android; 390 is a current iPhone.
VIEWPORTS = [
    ("320  iPhone SE", 320, 568),
    ("360  Android", 360, 800),
    ("390  iPhone 14", 390, 844),
    ("414  phablet", 414, 896),
    ("768  tablet", 768, 1024),
    ("1024 small laptop", 1024, 768),
]

# The element-level probe. `getBoundingClientRect` is in layout pixels, so the
# comparison against innerWidth is apples to apples. A 1px tolerance absorbs
# subpixel rounding on fractional layouts.
PROBE = """
() => {
  const doc = document.documentElement;
  // clientWidth, not innerWidth. Under mobile emulation `innerWidth` reports the
  // *visual* viewport, which Chrome zooms OUT to fit content that overflows - so
  // on an overflowing page it grows to match the overflow and the comparison
  // silently passes. Measured here: a 320px device reported innerWidth 644 on the
  // applications page, which was the overflow, not a broken viewport.
  // `documentElement.clientWidth` is the layout viewport and does not zoom.
  const vw = doc.clientWidth;
  // An element inside a deliberately scrollable box is not a page-overflow bug -
  // the kanban board and the mobile nav both scroll sideways on purpose, and
  // their children legitimately sit past the viewport edge. Without this the
  // report is dominated by nav links and every finding has to be hand-filtered.
  const inScroller = (el) => {
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if ((ox === 'auto' || ox === 'scroll') && p.scrollWidth > p.clientWidth) return true;
    }
    return false;
  };

  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right > vw + 1 || r.left < -1) {
      if (inScroller(el)) continue;
      // Report the outermost offender only: if a table overflows, every cell in
      // it also does, and a list of 60 <td>s buries the actual cause.
      if (offenders.some(o => o.el.contains(el))) continue;
      offenders.push({
        el: el,
        tag: el.tagName,
        cls: (typeof el.className === 'string' ? el.className : '') || '(none)',
        left: Math.round(r.left),
        right: Math.round(r.right),
        width: Math.round(r.width),
      });
    }
  }
  return {
    layoutWidth: vw,
    visualWidth: window.innerWidth,
    scrollWidth: doc.scrollWidth,
    overflowPx: doc.scrollWidth - doc.clientWidth,
    offenders: offenders.map(o => ({
      tag: o.tag, cls: o.cls, left: o.left, right: o.right, width: o.width,
    })),
  };
}
"""


def build_fixture() -> pathlib.Path:
    """Render every page, signed in, with enough data that tables and score rows
    are populated - an empty table cannot overflow, so an empty fixture would
    pass while the real app failed."""
    out = pathlib.Path(tempfile.gettempdir()) / "compass_responsive"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    # A unique file per run. A fixed name breaks the tool whenever a previous
    # run was interrupted: Windows keeps the SQLite handle open and the unlink
    # fails with WinError 32, which has nothing to do with what is being tested.
    db = pathlib.Path(tempfile.mkdtemp(prefix="compass_resp_")) / "compass.db"
    os.environ.update({
        "COMPASS_DB_URL": f"sqlite:///{db}",
        "COMPASS_SECRET_KEY": "responsive-check-secret-value",
        "COMPASS_PRELOAD_EMBEDDINGS": "false",
    })

    from fastapi.testclient import TestClient

    from app.auth import create_owner
    from app.db import SessionLocal, init_db
    from app.main import app
    from app.models import (Application, DiscoveredJob, JobPosting, JobSource,
                            ResumeVariant, Stage, utcnow)
    from app.schemas import (LLMAspiration, LLMCareerProfile, LLMExperience,
                             LLMSkill)
    from app.services import profile_service

    init_db()
    session = SessionLocal()
    user = create_owner(
        session, email="responsive@example.com",
        password="a-long-enough-passphrase", display_name="Hrisit Biswas",
    )

    profile_service.upsert_from_payload(session, user.id, LLMCareerProfile(
        full_name="Hrisit Biswas", email="h@example.com", phone="+91 98765 43210",
        location="Bangalore", links=[], headline="Senior Power BI Developer",
        summary="Six years building semantic models.",
        skills=[LLMSkill(name=n, category="hard", proficiency="strong", years=4.0)
                for n in ["Power BI", "DAX", "SQL", "dbt", "Snowflake"]],
        certifications=[],
        experiences=[LLMExperience(
            title="Senior Power BI Developer", company="Accenture Solutions Pvt Ltd",
            location="Bangalore", start_date="2021-01", end_date="", is_current=True,
            responsibilities=["Owned the semantic layer"],
            achievements=["Cut refresh from 42 min to 9 min"], scope_change_note="")],
        education=[], projects=[],
        aspiration=LLMAspiration(
            target_titles=["Analytics Engineer"], target_industries=[],
            target_companies=[], locations=["Remote"], remote_preference="remote",
            comp_min=0, comp_max=0, comp_currency="INR", non_negotiables=[]),
        unresolved_questions=[],
    ))
    session.add(ResumeVariant(
        user_id=user.id, label="Power BI forward - v3", flavor="power-bi-forward",
        content_md="Power BI and DAX.", raw_text="Power BI and DAX.",
        is_master=True, ats_score=88.0,
        ats_report={"grade": "Minor issues", "findings": [], "stats": {}}))

    for company, title, match, quality, verdict, stage in [
        ("Fivetran", "Analytics Engineer", 81.0, 84.0, "pass", Stage.INTERVIEW.value),
        ("Atlan", "Senior Data Engineer", 64.0, 61.0, "review", Stage.SCREEN.value),
        ("Databricks", "BI Platform Engineer", 41.0, 38.0, "templated", Stage.APPLIED.value),
    ]:
        posting = JobPosting(
            user_id=user.id, company=company, title=title, location="Remote - India",
            jd_text="Requirements\n- Power BI\n- dbt\n", source_name="greenhouse",
            source_type="api")
        session.add(posting)
        session.flush()
        session.add(Application(
            user_id=user.id, job_posting_id=posting.id, stage=stage,
            match_score=match, quality_score=quality, quality_verdict=verdict,
            cover_letter_text="Dear team,", tailored_resume_md="# CV",
            last_activity_at=utcnow()))

    source = JobSource(
        user_id=user.id, ats="greenhouse", board_token="fivetran",
        company_name="Fivetran", last_job_count=196, last_fetched_at=utcnow())
    session.add(source)
    session.flush()
    for ext, title, cur, clo, up in [
        ("1", "Senior Analytics Engineer, Data Platform", 58.0, 79.0, 21.0),
        ("2", "Staff Data Engineer", 72.0, 72.0, 0.0),
    ]:
        session.add(DiscoveredJob(
            user_id=user.id, source_id=source.id, external_id=ext,
            company="Fivetran", title=title, location="Remote - India",
            department="Data Platform", is_remote=True, url="https://example.com/",
            jd_text="Requirements\n- dbt\n", posted_at=utcnow(),
            match_score=cur, closeable_score=clo, is_deep_scored=True,
            keyword_score=cur, semantic_score=60.0,
            match_report={
                "uplift": up, "has_real_resume": True, "keyword_current": cur,
                "keyword_closeable": clo, "semantic": 60.0,
                "matched_profile_only": ["dbt", "snowflake"] if up else [],
                "matched_on_resume": ["power bi", "dax", "sql"],
                "top_missing": ["kafka", "spark"],
                "semantic_backend_is_model": True, "seniority": {}}))
    session.commit()

    shutil.copytree(PROJECT_ROOT / "app" / "static", out / "static")

    pages = {
        "dashboard": "/", "discover": "/discover", "applications": "/applications",
        "resumes": "/resumes", "tracker": "/tracker", "profile": "/profile",
        "settings": "/settings", "application_detail": "/applications/1",
        "login": "/login",
    }
    with TestClient(app, follow_redirects=True) as client:
        client.post("/login", data={
            "email": "responsive@example.com",
            "password": "a-long-enough-passphrase", "next": "/"})
        for name, path in pages.items():
            if name == "login":
                client.post("/logout")
            html = client.get(path).text.replace('"/static/', '"static/')
            (out / f"{name}.html").write_text(html, encoding="utf-8")

    session.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", action="store_true",
                        help="also write a real (uncropped) screenshot per page/viewport")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed.  pip install playwright", file=sys.stderr)
        return 2

    fixture = build_fixture()
    print(f"fixture: {fixture}\n")

    shots = fixture / "shots"
    if args.shots:
        shots.mkdir(exist_ok=True)

    pages = sorted(p.stem for p in fixture.glob("*.html"))
    failures: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        try:
            for label, width, height in VIEWPORTS:
                print(f"{'':2}{label}  ({width}x{height})")
                # `is_mobile` is deliberately off. It enables Chrome's
                # zoom-to-fit, which shrinks an overflowing page until it fits
                # and so hides the very thing being measured. With it off the
                # layout viewport is exactly the width requested, which is what
                # a CSS media query sees and what overflow is relative to.
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=2,
                    has_touch=width < 768,
                )
                page = context.new_page()
                for name in pages:
                    page.goto((fixture / f"{name}.html").as_uri())
                    page.wait_for_load_state("load")
                    result = page.evaluate(PROBE)

                    # Prove the emulation actually took, rather than trusting it.
                    # This guard is the whole reason to use playwright over a
                    # headless screenshot: it makes a viewport that did not apply
                    # a loud failure instead of a plausible-looking result.
                    if result["layoutWidth"] != width:
                        print(f"      ABORT: asked for {width}px, layout viewport "
                              f"is {result['layoutWidth']}px - emulation not applied")
                        return 2

                    overflow = result["overflowPx"]
                    if overflow <= 0:
                        print(f"      ok      {name}")
                    else:
                        print(f"      OVERFLOW {name}  +{overflow}px")
                        for off in result["offenders"][:4]:
                            print(f"                 {off['tag']}.{off['cls'][:44]}"
                                  f"  w={off['width']} right={off['right']}")
                        failures.append(f"{label} / {name}: +{overflow}px")

                    if args.shots:
                        page.screenshot(
                            path=str(shots / f"{name}-{width}.png"), full_page=True)
                context.close()
                print()
        finally:
            browser.close()

    print("=" * 62)
    if failures:
        print(f"{len(failures)} overflowing page/viewport combination(s):")
        for line in failures:
            print(f"  {line}")
        return 1
    print("No horizontal overflow at any tested width.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
