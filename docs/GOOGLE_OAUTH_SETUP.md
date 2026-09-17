# Google OAuth setup (Epic E)

One-time setup, about ten minutes. This is what lets Compass read your own inbox
to update the tracker and place interview slots on your calendar.

Compass requests two scopes and no more:

| Scope | Why | What it cannot do |
|---|---|---|
| `gmail.readonly` | Classify incoming application mail | Send, delete, or modify any message |
| `calendar.events` | Create interview events with reminders | Read or change calendars it wasn't given access to |

---

## 1. Create a Google Cloud project

1. Go to <https://console.cloud.google.com/projectcreate>.
2. Name it something recognisable — `compass-jobsearch` — and create it.
3. Make sure it is the selected project in the top bar before continuing.

## 2. Enable the two APIs

For each of these, click **Enable**:

- <https://console.cloud.google.com/apis/library/gmail.googleapis.com>
- <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>

## 3. Configure the OAuth consent screen

Go to **APIs & Services → OAuth consent screen**.

1. **User type: External.** "Internal" is only available on Workspace domains,
   and on a personal Gmail account it will not appear.
2. App name `Compass`, your own email as support and developer contact.
3. **Scopes** — add these two manually:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/calendar.events`
4. **Test users** — add your own Gmail address. This is the step people miss;
   while the app is in *Testing* mode, only listed test users can authorise it,
   and everyone else gets `access_denied`.
5. Leave publishing status as **Testing**. You do not need verification, and you
   should not request it — this app has exactly one user.

> **The 7-day refresh-token expiry.** In *Testing* mode Google expires refresh
> tokens after seven days. When Gmail sync starts failing, reconnect from
> Compass's Settings page. That is the trade for not going through app
> verification, and for a personal tool it is the right trade.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID**

- **Application type: Web application** (not "Desktop app" — the redirect below
  needs to be registered, and Compass runs a web server).
- Name: `Compass local`
- **Authorised redirect URIs** — add exactly:

  ```
  http://localhost:8000/google/callback
  ```

  It must match `COMPASS_GOOGLE_REDIRECT_URI` character for character. A trailing
  slash, `127.0.0.1` instead of `localhost`, or a different port all produce
  `redirect_uri_mismatch`.

Create it, then **Download JSON**.

## 5. Point Compass at it

```powershell
mkdir secrets
# move the downloaded file to:
#   secrets\google_client_secret.json
```

`secrets/` is gitignored. If you would rather not keep the file, put the two
values in `.env` instead:

```
COMPASS_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
COMPASS_GOOGLE_CLIENT_SECRET=...
```

Also set a real session secret, since it signs the cookie holding the OAuth
state parameter:

```
COMPASS_SECRET_KEY=<paste a long random string>
```

Generate one with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 6. Connect

Start Compass, open <http://localhost:8000/settings>, and click **Connect
Google**. You will see an "unverified app" warning — that is expected for an app
in Testing mode. Choose **Advanced → Go to Compass (unsafe)**.

Compass verifies the returned `state` against the value in your session cookie
before exchanging the code, and refuses the callback on a mismatch.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `redirect_uri_mismatch` | The registered URI differs from `COMPASS_GOOGLE_REDIRECT_URI`. Compare them exactly. |
| `access_denied` immediately | Your account is not in the consent screen's **Test users** list. |
| `Gmail API has not been used in project ... before or it is disabled` | Step 2 was skipped for that API. |
| Sync worked, then stopped after a week | Testing-mode refresh-token expiry. Reconnect from Settings. |
| `OAuth state did not match this session` | You restarted the server mid-flow (the session cookie key changed), or reused an old callback URL. Start again from Settings. |
| `insecure_transport` errors | `oauth.py` sets `OAUTHLIB_INSECURE_TRANSPORT` for loopback HTTP. If you changed the redirect to a non-localhost host, use HTTPS. |

## Revoking access

Disconnecting in Compass deletes the local token only. To revoke on Google's
side, go to <https://myaccount.google.com/permissions> and remove Compass.
