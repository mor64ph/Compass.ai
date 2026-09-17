"""Transport- and browser-level hardening.

Three concerns, all of which became load-bearing the moment Compass could be
reached by anyone other than the person who wrote it:

* **Response headers** - a Content-Security-Policy that forbids inline script and
  eval, so injected text cannot become executable. Compass renders user-supplied
  résumé and JD text on nearly every page, which is exactly the input an attacker
  controls.
* **Login throttling** - the password hash is deliberately expensive (~100ms), but
  that alone does not stop a patient attacker. This caps attempts per address and
  per source address.
* **Secret-key enforcement** - `app/config.py` ships a placeholder key, and that
  file is in a public repository. A deployment that keeps the default is one where
  anyone who reads the repo can forge a session cookie for any account. Refusing
  to boot is the only safe response; see `assert_deployable`.
"""

from __future__ import annotations

import logging
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

DEFAULT_SECRET_KEY = "dev-only-change-me"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


# --------------------------------------------------------------------------
# Response headers
# --------------------------------------------------------------------------

# `script-src 'self'` with no 'unsafe-inline' and no 'unsafe-eval' is the whole
# point of moving every handler into app/static/js/app.js. htmx is served from
# our own origin for the same reason: a CDN is a third party that can change the
# bytes it serves, and Compass holds career data.
#
# `style-src` does allow 'unsafe-inline' because the templates use inline style
# attributes for layout. An inline style is a far weaker vector than inline
# script, and removing several hundred of them buys little.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    ]
)

STATIC_HEADERS = {
    "Content-Security-Policy": CSP,
    # Belt and braces with frame-ancestors, for older browsers.
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Don't leak the path of an internal page - which can name a company you are
    # applying to - in the Referer of an outbound click.
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    # Nothing here needs any of these.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the headers above to every response.

    `https_only` also sends HSTS. It is deliberately tied to the same setting
    that marks the session cookie Secure, because sending HSTS from a deployment
    reachable over plain HTTP would lock users out of it.
    """

    def __init__(self, app, *, https_only: bool = False) -> None:
        super().__init__(app)
        self.https_only = https_only

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for header, value in STATIC_HEADERS.items():
            response.headers.setdefault(header, value)
        if self.https_only:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


# --------------------------------------------------------------------------
# Login throttling
# --------------------------------------------------------------------------


class LoginThrottle:
    """Sliding-window limiter on failed logins, keyed independently by email and
    by client address.

    Both keys matter and neither is sufficient. Per-email alone lets one host
    spray a whole address list; per-address alone lets a botnet grind a single
    account. A *successful* login clears the email's history, so a legitimate
    user who mistypes twice and then gets it right is never penalised.

    In-process and not shared between workers, which is the right scope for a
    single-machine deployment. It is a brute-force brake, not a DDoS defence -
    that belongs at the proxy.
    """

    def __init__(self, *, limit: int = 8, window_seconds: float = 900.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        return [t for t in self._failures.get(key, []) if now - t < self.window]

    def retry_after(self, *, email: str, client: str) -> int:
        """Seconds the caller must wait, or 0 when the attempt may proceed."""
        now = time.monotonic()
        with self._lock:
            waits = [0]
            for key in self._keys(email, client):
                hits = self._recent(key, now)
                self._failures[key] = hits
                if len(hits) >= self.limit:
                    waits.append(int(self.window - (now - hits[0])) + 1)
            return max(waits)

    def record_failure(self, *, email: str, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in self._keys(email, client):
                self._failures[key] = self._recent(key, now) + [now]

    def record_success(self, *, email: str, client: str) -> None:
        with self._lock:
            for key in self._keys(email, client):
                self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()

    @staticmethod
    def _keys(email: str, client: str) -> tuple[str, ...]:
        return (f"email:{(email or '').strip().lower()}", f"addr:{client or '?'}")


login_throttle = LoginThrottle()


def client_address(request: Request) -> str:
    """Best-effort source address.

    `X-Forwarded-For` is honoured because a free-tier host will always sit behind
    a proxy, and without it every request appears to come from the proxy and the
    per-address limit would throttle all users at once. It is client-controlled,
    so it is a convenience for honest traffic rather than a security boundary -
    which is why the per-email key exists alongside it.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


# --------------------------------------------------------------------------
# Deployment safety
# --------------------------------------------------------------------------


def assert_deployable(*, secret_key: str, host: str) -> None:
    """Refuse to start a non-local deployment that still has the shipped key.

    `compass_secret_key` has a placeholder default so that `git clone && run`
    works for a local trial. That default is public. If it is still in place while
    bound to anything other than loopback, every session cookie in the deployment
    is forgeable by anyone who has read the repository - so this is a hard stop
    rather than a warning.
    """
    if secret_key and secret_key != DEFAULT_SECRET_KEY:
        return
    if host in LOCAL_HOSTS:
        logger.warning(
            "COMPASS_SECRET_KEY is still the shipped default. That is survivable on "
            "localhost, but set it before exposing Compass to anything else."
        )
        return
    raise RuntimeError(
        f"COMPASS_SECRET_KEY is still the shipped default while bound to {host!r}.\n"
        "That key is published in the public repository, so every session cookie "
        "would be forgeable by anyone who reads it.\n\n"
        "Generate one and put it in .env:\n"
        '    python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
    )
