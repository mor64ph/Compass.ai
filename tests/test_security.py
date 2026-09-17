"""Security hardening: response headers, login throttling, upload limits, and
the refusal to deploy with the shipped secret key.

These are regression tests for defences, which means the assertions are mostly
negative - "this header is present", "this attempt is refused". A defence that
silently stops working looks exactly like one that is working, so it needs a test
more than a feature does.
"""

from __future__ import annotations

import io

import pytest

from app.security import (
    CSP,
    DEFAULT_SECRET_KEY,
    LoginThrottle,
    assert_deployable,
    client_address,
    login_throttle,
)


@pytest.fixture(autouse=True)
def _clear_throttle():
    """The throttle is process-global, so one test's failures would otherwise
    lock out the next test's login."""
    login_throttle.reset()
    yield
    login_throttle.reset()


# --------------------------------------------------------------------------
# Response headers
# --------------------------------------------------------------------------


def test_every_response_carries_the_csp(client):
    response = client.get("/")
    assert response.headers["content-security-policy"] == CSP


def test_csp_forbids_inline_script_and_eval():
    """The reason every handler was moved into app/static/js/app.js. If either
    of these creeps back in, injected résumé text becomes executable."""
    assert "script-src 'self'" in CSP
    assert "'unsafe-inline'" not in CSP.split("script-src")[1].split(";")[0]
    assert "'unsafe-eval'" not in CSP


def test_csp_allows_inline_style_deliberately():
    """Templates use style attributes for layout. Documented as an accepted
    trade so a future reader does not assume it was an oversight."""
    style = CSP.split("style-src")[1].split(";")[0]
    assert "'unsafe-inline'" in style


def test_clickjacking_and_sniffing_headers_present(client):
    headers = client.get("/").headers
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


def test_referrer_is_not_leaked(client):
    """An internal path names the company being applied to; it must not travel
    in the Referer of an outbound click."""
    assert client.get("/").headers["referrer-policy"] == "no-referrer"


def test_error_pages_also_carry_headers(client):
    """The headers middleware wraps the session middleware, so a 404 raised deep
    inside the stack is still covered."""
    response = client.get("/applications/999999")
    assert response.status_code == 404
    assert "content-security-policy" in response.headers


def test_no_hsts_when_serving_plain_http(client):
    """HSTS from an http deployment would lock users out of it."""
    assert "strict-transport-security" not in client.get("/").headers


def test_htmx_is_served_from_our_own_origin(client):
    """Self-hosted so the CSP needs no CDN exception, and so a third party
    cannot change the bytes served to a page holding career data."""
    assert client.get("/static/js/htmx.min.js").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    assert "unpkg.com" not in client.get("/login").text


# --------------------------------------------------------------------------
# Login throttling
# --------------------------------------------------------------------------


def test_repeated_failures_are_eventually_refused(client):
    creds = {"email": "owner@example.com", "password": "wrong", "next": "/"}
    for _ in range(login_throttle.limit):
        client.post("/login", data=creds)

    blocked = client.post("/login", data=creds)
    assert "Too many failed attempts" in blocked.text


def test_throttle_does_not_reveal_whether_the_account_exists(client):
    """The lockout message must be identical for a real and an unknown address,
    or the throttle becomes the oracle the login page carefully is not."""
    for email in ("owner@example.com", "nobody@example.com"):
        login_throttle.reset()
        data = {"email": email, "password": "wrong", "next": "/"}
        for _ in range(login_throttle.limit):
            client.post("/login", data=data)
        assert "Too many failed attempts" in client.post("/login", data=data).text


def test_a_correct_password_clears_the_users_failures():
    """Two typos then success must not leave the account part-way to a lockout."""
    throttle = LoginThrottle(limit=3)
    for _ in range(2):
        throttle.record_failure(email="a@b.c", client="1.2.3.4")
    throttle.record_success(email="a@b.c", client="1.2.3.4")
    for _ in range(2):
        throttle.record_failure(email="a@b.c", client="1.2.3.4")
    assert throttle.retry_after(email="a@b.c", client="1.2.3.4") == 0


def test_throttle_keys_on_address_as_well_as_email():
    """Per-email alone would let one host spray a whole address list."""
    throttle = LoginThrottle(limit=3)
    for i in range(3):
        throttle.record_failure(email=f"victim{i}@example.com", client="9.9.9.9")
    assert throttle.retry_after(email="fresh@example.com", client="9.9.9.9") > 0
    assert throttle.retry_after(email="fresh@example.com", client="1.1.1.1") == 0


def test_throttle_window_expires():
    throttle = LoginThrottle(limit=1, window_seconds=0.01)
    throttle.record_failure(email="a@b.c", client="1.2.3.4")
    assert throttle.retry_after(email="a@b.c", client="1.2.3.4") > 0
    import time

    time.sleep(0.02)
    assert throttle.retry_after(email="a@b.c", client="1.2.3.4") == 0


def test_forwarded_for_is_used_but_only_the_first_hop():
    class _Req:
        headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
        client = None

    assert client_address(_Req()) == "203.0.113.9"


# --------------------------------------------------------------------------
# Upload limits
# --------------------------------------------------------------------------


def test_oversized_upload_is_refused(client):
    """Streamed and capped rather than read-then-measured, so the process is not
    holding the whole body when it decides to reject it."""
    from app.config import get_settings

    limit = get_settings().compass_max_upload_mb * 1024 * 1024
    payload = b"x" * (limit + 1024)
    response = client.post(
        "/resumes/upload",
        files={"file": ("big.txt", io.BytesIO(payload), "text/plain")},
        data={"label": "huge", "flavor": "base"},
    )
    assert response.status_code == 303
    assert client.get("/resumes").text.count("huge") == 0


def test_unreadable_file_types_are_refused_server_side(client):
    """The accept attribute on the input is a file-picker convenience; a
    hand-rolled POST ignores it."""
    response = client.post(
        "/resumes/upload",
        files={"file": ("payload.exe", io.BytesIO(b"MZ\x90\x00"), "application/octet-stream")},
        data={"label": "nope", "flavor": "base"},
    )
    assert response.status_code == 303
    assert client.get("/resumes").text.count("nope") == 0


def test_a_normal_resume_still_uploads(client):
    """The guards above must not have closed the front door."""
    response = client.post(
        "/resumes/upload",
        files={"file": ("cv.txt", io.BytesIO(b"Jane Doe\njane@example.com\n"), "text/plain")},
        data={"label": "fine", "flavor": "base"},
    )
    assert response.status_code == 303
    assert "/resumes/" in response.headers["location"]


def test_upload_filename_cannot_escape_the_upload_directory(client):
    """Stored under a generated uuid; only the suffix is taken from user input."""
    client.post(
        "/resumes/upload",
        files={"file": ("../../../evil.txt", io.BytesIO(b"hello there"), "text/plain")},
        data={"label": "traversal", "flavor": "base"},
    )
    from app.config import get_settings

    upload_dir = get_settings().upload_dir
    assert not (upload_dir.parent.parent / "evil.txt").exists()


# --------------------------------------------------------------------------
# Deployment safety
# --------------------------------------------------------------------------


def test_default_secret_key_is_allowed_on_loopback():
    assert_deployable(secret_key=DEFAULT_SECRET_KEY, host="127.0.0.1")


def test_default_secret_key_refuses_to_bind_publicly():
    """The default is published in the repository, so a deployment keeping it has
    forgeable sessions for every account."""
    with pytest.raises(RuntimeError, match="forgeable"):
        assert_deployable(secret_key=DEFAULT_SECRET_KEY, host="0.0.0.0")


def test_empty_secret_key_also_refuses():
    with pytest.raises(RuntimeError):
        assert_deployable(secret_key="", host="0.0.0.0")


def test_a_real_secret_key_deploys_anywhere():
    assert_deployable(secret_key="P8xq-generated-value-of-real-length", host="0.0.0.0")


# --------------------------------------------------------------------------
# Session cookie
# --------------------------------------------------------------------------


def test_session_cookie_is_httponly_and_lax(client):
    """HttpOnly keeps it away from script; Lax is what makes the absence of CSRF
    tokens defensible, because the browser will not attach it to a cross-site
    POST."""
    response = client.post(
        "/login",
        data={"email": "owner@example.com", "password": "a-long-enough-passphrase",
              "next": "/"},
    )
    cookie = response.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


def test_session_cookie_has_a_bounded_lifetime(client):
    response = client.post(
        "/login",
        data={"email": "owner@example.com", "password": "a-long-enough-passphrase",
              "next": "/"},
    )
    assert "Max-Age=" in response.headers.get("set-cookie", "")


# --------------------------------------------------------------------------
# Template rendering against untrusted / awkward data
# --------------------------------------------------------------------------


def test_gap_report_renders_its_findings():
    """Regression: `gap` is a plain dict, so Jinja resolved `gap.items` to
    dict.items - a bound method, always truthy and with no length. The section
    therefore raised for every populated gap report, which unit tests missed
    because none of them rendered this partial with real data.
    """
    from app.web import templates

    template = templates.get_template("partials/gap_report.html")
    html = template.render(
        gap={
            "summary": "Strong on modelling, light on orchestration.",
            "items": [
                {"kind": "gap", "severity": "high", "detail": "No Airflow",
                 "evidence": "", "suggested_fix": "Say what you used instead."}
            ],
            "honest_gaps": ["Airflow"],
            "strongest_matches": ["DAX"],
        }
    )
    assert "1 finding(s)" in html
    assert "No Airflow" in html
    assert "built-in_function" not in html


def test_gap_report_survives_a_missing_items_key():
    from app.web import templates

    html = templates.get_template("partials/gap_report.html").render(
        gap={"summary": "only a summary"}
    )
    assert "finding(s)" not in html
    assert "only a summary" in html


def test_resume_text_is_escaped_not_executed(client):
    """Autoescape plus `script-src 'self'` are the two layers here. This asserts
    the first: a script tag in uploaded text must arrive inert."""
    client.post(
        "/resumes/upload",
        files={
            "file": (
                "xss.txt",
                io.BytesIO(b"Jane <script>alert(1)</script> Doe\njane@example.com\n"),
                "text/plain",
            )
        },
        data={"label": "xss-probe", "flavor": "base"},
    )
    body = client.get("/resumes").text
    assert "<script>alert(1)</script>" not in body


# --------------------------------------------------------------------------
# Status is never colour alone
# --------------------------------------------------------------------------


def test_score_word_pairs_with_every_score_class():
    """`score_class` encodes a judgement as a hue; `score_word` says it. A score
    shown only in green or only in red is unreadable to a reader with
    deuteranopia and unreadable in a greyscale screenshot, so the stylesheet's
    `.judgement` pairs a dot with this text and the two must stay in step.
    """
    from app.web import _score_class, _score_word

    for score, klass, word in [
        (95, "good", "strong"),
        (75, "good", "strong"),
        (74, "warn", "needs work"),
        (55, "warn", "needs work"),
        (54, "bad", "weak"),
        (0, "bad", "weak"),
    ]:
        assert _score_class(score) == klass, score
        assert _score_word(score) == word, score


def test_score_word_survives_junk():
    from app.web import _score_word

    assert _score_word(None) == "weak"
    assert _score_word("not a number") == "not scored"


def test_dashboard_states_carry_a_word_not_only_a_colour(client):
    """Renders the real template and asserts the judgement text is present
    beside the coloured value."""
    body = client.get("/").text
    assert 'class="judgement' in body
    assert any(w in body for w in ("strong", "needs work", "weak"))
