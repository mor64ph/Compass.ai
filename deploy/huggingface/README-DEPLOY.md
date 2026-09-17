# Deploying to Hugging Face Spaces

> ## This path is closed on the free plan
>
> **Verified 18-Sep-2026.** The new-Space form marks the Docker SDK **Paid**:
> *"Gradio and Docker Spaces require a paid plan. Static Spaces stay free for
> everyone."* Static Spaces serve files only and cannot run Python, so Compass
> cannot be deployed here without a PRO subscription.
>
> **Use [../render/README-DEPLOY.md](../render/README-DEPLOY.md) instead.**
>
> This guide is kept because the configuration is finished and correct — the
> Dockerfile, the Space README and the steps below all work. If you have PRO, or
> if HF changes the plan again, it is ready to use. On 2 vCPU / 16 GB it also
> runs the *real* embedding model, which the Render deployment cannot.

2 vCPU, 16 GB RAM, public HTTPS URL. About 25 minutes, most of it waiting for
the image to build.

> **On tunnelling**, since it is the obvious alternative: it is blocked on this
> network. The corporate Forcepoint filter returns an HTML block page for
> `api.trycloudflare.com`, so `cloudflared` cannot create a quick tunnel at all.
> ngrok and similar fail the same way — enterprise filters block tunnelling
> services by category.

---

## 1. Create the Space

1. Sign up at [huggingface.co/join](https://huggingface.co/join) — email only.
2. Go to [huggingface.co/new-space](https://huggingface.co/new-space).
   - **Space name:** `compass`
   - **License:** MIT
   - **SDK: Docker** → *Blank*  ← if this is unavailable, stop here
   - **Hardware:** CPU basic (free)
   - **Visibility:** Public
3. Create it. Note the git URL: `https://huggingface.co/spaces/<you>/compass`

## 2. Push the code

A Space is its own git repo. Compass's code goes in at the root, with this
directory's `Dockerfile` and `README.md` as the top-level ones.

```bash
cd /tmp
git clone https://huggingface.co/spaces/<you>/compass hf-compass
git clone https://github.com/mor64ph/ai-jobsearch-engine.git src

cd hf-compass
# The app, and the two scripts the container references.
cp -r ../src/app ../src/scripts ../src/requirements.txt .
# The Space-specific Dockerfile and README replace the repo's own.
cp ../src/deploy/huggingface/Dockerfile .
cp ../src/deploy/huggingface/README.md .

git add -A
git commit -m "Compass"
git push
```

You will be asked for a token, not your password: create one at
[huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) with
**write** access.

> **Do not copy `.env`, `compass.db`, `data/` or `secrets/`.** The commands above
> name only what belongs in the Space, rather than copying everything and relying
> on an ignore file — a Space repo is public, and a secret pushed to it is public
> the moment it lands and stays in the history afterwards.

## 3. Set the secrets

Space → **Settings** → **Variables and secrets**. Secrets are injected as
environment variables at runtime.

| Name | Kind | Value |
|---|---|---|
| `COMPASS_SECRET_KEY` | **Secret** | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `GEMINI_API_KEY` | **Secret** | your key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `COMPASS_LLM_PROVIDER` | Variable | `gemini` |
| `COMPASS_GEMINI_MODEL` | Variable | `gemini-3.5-flash` |

The secret key is mandatory: Compass refuses to start without a real one when
bound to anything but loopback, because the placeholder in `app/config.py` is in
a public repository and would leave every session cookie forgeable.

`COMPASS_DEMO_MODE` is already `true` in the Space Dockerfile — that is what
seeds the account whose credentials appear on the sign-in page. Without it a
visitor hits the invite-only wall and sees nothing.

## 4. Wait for the build

Space → **Logs** → *Build*. **10–20 minutes** on the first run: compiling
wheels, then baking the embedding model into the image so the first visitor does
not wait 90 seconds for a 90 MB download.

Then **Logs → Container**, and wait for:

```
Compass startup complete
Seeded the demo account demo@compass.local - not an admin, $2.00/month cap
```

Your Space is at `https://<you>-compass.hf.space`.

## 5. Check it

```
[ ] Sign-in page shows the demo credentials
[ ] They work, and land you on a populated dashboard
[ ] Résumés shows a variant with a real ATS score
[ ] Applications lists three at different stages
[ ] Discover → add board `fivetran` (Greenhouse) → Rank
[ ] One AI action completes — Gap report on any application
[ ] Open it on your phone
```

The Discover step is the one worth doing: it exercises ATS ingestion, the
deterministic scorer and the embedding model together, which is most of the app
in one click.

---

## Operating notes

**It sleeps after 48 hours with no visitors** and cold-starts in roughly a
minute — the embedding model has to load. Fine for a portfolio link; worth
knowing before you send it to someone.

**Data resets on every restart.** Free Spaces have no persistent disk. The demo
seed makes that survivable rather than merely broken: every restart produces a
clean, populated instance. Persistent storage is a paid add-on, and the
documented free alternative is syncing SQLite to a Dataset repo, which is not
built here.

**Gemini's free tier is 20 requests per day per model**, shared by every visitor
because it is one key. A demo that gets attention will exhaust it. When it does,
everything deterministic still works. Switch `COMPASS_GEMINI_MODEL` for a fresh
allowance.

**The container is not a secret store.** Anyone can read the Dockerfile in a
public Space. Secrets set in Settings are injected at runtime and are not in the
image — which is why they go there and not into the Dockerfile.

---

## The fallback, which is what you actually want

Docker Spaces are paid, so this is built and documented at
**[../render/README-DEPLOY.md](../render/README-DEPLOY.md)**: Render's free tier,
no card, `deploy/render/requirements-slim.txt` at 149 MB instead of 1,279 MB.

Worth saying plainly: it is a materially worse demo of *this particular* app.
512 MB has no room for torch, so scoring runs on the lexical backend, and the
quality gate's divergence signal — the thing the whole tool is built around — is
the place that costs the most. The Render guide quantifies it rather than waving
at it.
