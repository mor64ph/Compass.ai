# Security

Compass holds a complete career history, uploaded résumés, and — if you connect
it — read access to a Gmail account. This document says what it defends against,
what it does not, and what you must do before exposing it to anyone.

## Threat model

Compass is designed for **one instance, one operator, a handful of invited
users**. The attackers it takes seriously, in order:

1. **An anonymous visitor who found the URL.** Every route except `/login`,
   `/setup` and `/invite/{token}` requires a session.
2. **An invited user poking at other people's data.** Every aggregate root
   carries a `user_id` and every lookup goes through `require_owned()`.
3. **Someone who has read this repository** — it is public — and is looking for a
   default credential or a predictable secret.
4. **A malicious document or job description**, since both are attacker-supplied
   input that gets parsed and rendered.

It is explicitly **not** designed to survive a determined attacker with network
access to the host, or to be a multi-tenant SaaS. See "Out of scope".

## What is implemented

### Authentication
- Passwords hashed with `hashlib.scrypt`, n=2¹⁵, r=8 — ~100 ms per verification,
  ~32 MB of memory, deliberately hostile to offline cracking. Parameters are
  stored with the hash so they can be raised later without invalidating anyone.
- Verification is constant-time (`hmac.compare_digest`).
- An unknown email still runs the KDF against a dummy hash, so response timing
  does not reveal which addresses have accounts.
- One error message for every failure mode.
- Failed logins are throttled on **both** the email and the source address —
  8 per 15 minutes. A correct password clears that email's history, so typos cost
  nothing. Checked *before* the KDF runs, or the lockout would be its own DoS.
- No self-service signup. Invites are `secrets.token_urlsafe(32)`, single-use,
  14-day expiry.
- `/setup` refuses once any account exists, so it cannot be used to escalate.
- The session is cleared on login, which prevents session fixation.

### Session
- Signed cookie, `HttpOnly`, `SameSite=Lax`, bounded `Max-Age` (14 days default).
- `Secure` is set when `COMPASS_HTTPS_ONLY=true`. It must be false for local
  http, or the browser discards the cookie and login silently does nothing.
- **`SameSite=Lax` is what makes the absence of CSRF tokens defensible.** The
  browser will not attach the cookie to a cross-site POST, which is the vector
  CSRF tokens exist to close. Every state change in Compass is a POST. If a
  GET-triggered state change is ever added, this reasoning breaks and real CSRF
  tokens become necessary.

### Tenant isolation
- `user_id` on every aggregate root; `require_owned()` on every lookup.
- A cross-account request returns **404, not 403** — 403 confirms the record
  exists, which is itself a disclosure.
- `tests/test_tenancy.py` holds 32 HTTP-layer tests covering cross-account reads,
  writes, deletes, and admin-only surfaces.

### Browser-side
- A Content-Security-Policy with `script-src 'self'` — **no `unsafe-inline`, no
  `unsafe-eval`**. Every event handler lives in `app/static/js/app.js`, declared
  from templates via `data-` attributes. This is why you will not find an
  `onclick=` or an `hx-on:` anywhere in the templates.
- htmx is **self-hosted**, not loaded from a CDN. A CDN is a third party that can
  change the bytes it serves to a page displaying your career history.
- `X-Frame-Options: DENY` + `frame-ancestors 'none'`, `nosniff`,
  `Referrer-Policy: no-referrer` (an internal path names the company you are
  applying to), and a `Permissions-Policy` that turns off APIs nothing uses.
- Jinja2 autoescaping is on and no template uses `|safe`.

`style-src` does permit `'unsafe-inline'`, because the templates use inline style
attributes for layout. That is an accepted trade, not an oversight: an inline
style is a far weaker vector than inline script.

### Input handling
- Uploads are **streamed and capped** at `COMPASS_MAX_UPLOAD_MB` (default 10).
  Reading first and measuring afterwards would hold a 2 GB body in memory before
  rejecting it.
- Extensions are whitelisted server-side. The `accept` attribute on the input is
  a file-picker convenience; a hand-rolled POST ignores it.
- Stored files are named `{uuid4().hex}{suffix}` — only the suffix comes from
  user input, so a filename cannot traverse out of the upload directory.
- All database access is through the SQLAlchemy ORM. There is no raw SQL.

### Deployment
- `app/security.py::assert_deployable` **refuses to start** when
  `COMPASS_SECRET_KEY` is still the shipped placeholder and the app is bound to
  anything other than loopback. That default is in this public repository; a
  deployment keeping it has forgeable session cookies for every account,
  including the admin. This is a hard stop, not a warning.

## Out of scope — residual risks

Stated rather than left to be discovered.

| Risk | Status |
|---|---|
| **Malicious DOCX/PDF internals** — XML entity expansion, decompression bombs | Not specifically defended. The 10 MB cap bounds input size but not decompressed size. `pdfplumber` and `python-docx` are the parsing boundary; keep them patched. |
| **Prompt injection via a job description** | Not preventable, but the blast radius is bounded by design: the model has no tools beyond grounded search, cannot reach the database, and **cannot submit anything**. The worst case is bad text that you review before sending — which is exactly what the no-autosubmit rule and the quality gate exist for. |
| **Encryption at rest** | Not implemented. `compass.db` is a plain SQLite file. Use full-disk encryption. |
| **Rate limiting beyond login** | In-process only, and per-worker. Real request-rate limiting belongs at the reverse proxy. |
| **Audit log** | Application events are recorded; authentication events only reach the application log. |
| **Password reset** | Deliberately absent. An admin re-invites instead; there is no email-based reset flow to attack. |
| **Secret rotation** | Changing `COMPASS_SECRET_KEY` invalidates every session. That is the intended behaviour, and the intended response to a suspected compromise. |
| **Dependency vulnerabilities** | Not automated. Run `pip list --outdated` periodically; `torch` and `pdfplumber` carry the largest transitive surface. |

## Before you expose this to anyone

```
[ ] COMPASS_SECRET_KEY set to a real generated value
      python -c "import secrets; print(secrets.token_urlsafe(48))"
[ ] COMPASS_HTTPS_ONLY=true, and actually serving over TLS
[ ] The owner account created, so /setup is closed
[ ] .env, compass.db, compass.db-wal and data/ confirmed absent from git
      git ls-files | Select-String "\.env$|\.db"     # must return nothing
[ ] Full-disk encryption on the host
[ ] A backup of compass.db that is not on the same disk
```

The WAL file matters more than it looks: `compass.db-wal` contains real database
pages. `.gitignore` covers `*.db-*` for exactly that reason.

## Reporting something

This is a personal project, not a funded product. Open an issue — or if you would
rather not do that publicly, say so in an issue with no detail and I will find
another channel. Please do not test against an instance that is not your own.
