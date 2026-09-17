# Choosing an AI provider

Compass's prompts were written against Claude, but the LLM layer is not
Claude-shaped. `app/llm/providers/` holds one adapter per provider; everything
above it — prompt versioning, the response cache, spend caps, Pydantic
validation — is shared.

Set `COMPASS_LLM_PROVIDER` in `.env` to `gemini`, `ollama`, `anthropic`, or leave
it `auto`. Settings shows which are configured and re-checks without a restart.

## The three options

| | Cost | Speed | Quality for Compass | Catch |
|---|---|---|---|---|
| **Gemini** (default) | Free tier, no card | Fast | Good — solid structured output | **20 requests/day per model**; no web research |
| **Ollama** | Free, forever | **Minutes per generation on this laptop** | Weakest on the honesty rules | Slow; no web research |
| **Anthropic** | Paid, ~$1/verify run | Fast | Best — prompts are tuned for it | Costs money |

**Verified on a real free-tier key: 10 of Compass's 11 AI features work on
Gemini's free tier.** The exception is cited company research, which needs
Google Search grounding — a paid feature. Everything else — résumé parsing,
intake, gap reports, bullet rewrites, tailoring, email classification, prep
briefs, mock interviews — works.

`auto` picks the first configured one in the order Gemini → Ollama → Anthropic:
free and keyless before paid.

---

## Gemini (recommended default)

1. Go to <https://aistudio.google.com/apikey> and create a key. No card.
2. Put it in `.env`:

   ```
   GEMINI_API_KEY=...
   COMPASS_LLM_PROVIDER=gemini
   ```

3. Open Settings and confirm it shows **ready**.

The default is **`gemini-3.6-flash`**, verified working with structured output on
a fresh free-tier key.

### Things that bit us, so they don't bite you

**Being listed in `/models` does not mean you can use it.** `gemini-2.5-flash`
appears in the model list and reports `generateContent` among its supported
methods — then returns 404 with *"no longer available to new users. Please update
your code to use models/gemini-3.6-flash"*. Settings shows the live list, but the
only real test is a call. If a model 404s, the error now carries Google's own
message, which usually names the replacement.

**The free tier is 20 requests per DAY, per model.** Not per minute — the quota
id is `GenerateRequestsPerDayPerProjectPerModel-FreeTier`. Compass makes 2–3
calls per user action, so that is roughly **seven tailored applications a day**
before a model is spent.

The saving grace is "PerModel": each model carries its own allowance. When one
runs out, set `COMPASS_GEMINI_MODEL` to another and carry on —
`gemini-3.5-flash`, `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` were all
confirmed working. The adapter detects a per-day exhaustion and says exactly
this, rather than telling you to wait a moment for a quota that resets at
midnight.

Short per-minute limits also exist; those the adapter simply waits out, honouring
the `retryDelay` Google returns, up to 70 seconds.

**Google Search grounding is not on the free tier.** Verified across three
models: plain generation succeeds on all of them, while the identical call with
`google_search` returns 429. So **cited company research does not work on free
Gemini** — that is the one Compass feature the free tier cannot do. Everything
else works. Compass degrades gracefully: prep briefs and tailoring both treat
research as optional and carry on without it.

If you want research, enable billing on the Google Cloud project (flash models
cost fractions of a cent), or switch to Anthropic for that step.

**A corporate TLS-inspecting proxy will intermittently break HTTPS.** On the
machine this was built on, the same Gemini URL succeeded and then failed minutes
later with `certificate verify failed: self-signed certificate in certificate
chain`. `app/tls.py` points Python at the Windows certificate store, which does
contain the corporate root, and the adapter retries TLS errors. Note this is
*more* correct than the bundled CA list on a managed machine — it is not the same
as disabling verification, which would be the wrong fix.

### Handled for you

- **Schema dialect.** `responseSchema` is an OpenAPI subset and rejects
  `additionalProperties`, which Compass's hardened schema always sets.
  `to_gemini_schema()` strips it rather than weakening the canonical schema.
- **The key travels in the `x-goog-api-key` header**, not `?key=`. Query strings
  end up in proxy and server logs; headers usually don't.

Grounded company research works — Gemini's `google_search` tool. Only URLs it
actually returns are ever shown, so a citation can't be invented.

---

## Ollama (no key, ever)

The answer to *"what happens when my API access ends"*: Compass keeps working,
free, offline, with résumé text never leaving the machine. That's consistent with
the rest of the design — embeddings already run locally for the same reason.

```powershell
# 1. Install from https://ollama.com  (runs as a background service)
# 2. Fetch a small model
ollama pull llama3.2:3b
```

```
COMPASS_LLM_PROVIDER=ollama
COMPASS_OLLAMA_MODEL=llama3.2:3b
```

### Be realistic about this on a laptop

Measured on the machine this was built on — i5-1145G7, 4 cores, no usable GPU:

- Compass sends **5–8k tokens** per call (career profile + résumé + JD) and asks
  for **1–2k tokens** of structured JSON back.
- At CPU speeds that's roughly **5–10 minutes per tailored application**.
- A 3B model is specified rather than 7B/8B deliberately. Larger is slower, and
  RAM is the binding constraint.

Two settings matter more than the model choice:

- **`COMPASS_OLLAMA_NUM_CTX=8192`.** Ollama defaults to 2048. At that size
  Compass's prompt is silently truncated — the JD falls off the end and the model
  confidently writes about nothing. This is the single most damaging
  misconfiguration available here.
- **Structured output** is sent as a JSON *schema*, not `format: "json"`. That
  constrains decoding, which is what makes a small model's structured output
  usable at all.

### What gets worse, specifically

Not just "lower quality" — the failure modes matter:

- **The anti-fabrication instructions are followed less reliably.** The prompts
  repeatedly say numbers may come only from the profile. A 3B model complies less
  often than Claude. Since a fabricated metric ends up in a real application,
  read `claims_avoided` and check every number against your own CV.
- **No web search.** The adapter refuses grounded research rather than letting a
  local model invent citations. Generate prep briefs without research, or switch
  provider for that step.
- **Long structured output is where small models break.** If you see *"returned a
  response that did not match the expected shape"*, that's the cause.

The deterministic half — ATS checker, JD scoring, the quality gate, the tracker —
is unaffected. It never used a model.

---

## Anthropic

```
ANTHROPIC_API_KEY=sk-ant-...
COMPASS_LLM_PROVIDER=anthropic
```

Or leave the key blank and run `ant auth login`; the SDK resolves an OAuth
profile automatically.

Keep this working even if it isn't your default. It's the quality benchmark —
the only way to judge whether a cheaper provider is *good enough* is to have
something to compare against. Run `verify-llm.cmd` once against Claude on your
real profile and keep the output.

---

## Adding another provider

Implement `LLMProvider` from `app/llm/providers/base.py` — `available()`,
`describe()`, `complete()` — and register it in `app/llm/providers/__init__.py`.
Nothing else in the app should need to change. `tests/test_providers.py` shows
the shape: mock the HTTP, assert the request body, and assert that each error
status becomes a message that says what to do.

Two rules worth keeping:

- **`available()` must not make a network call.** It runs on page loads. Ollama's
  probe is cached per process for exactly this reason.
- **Errors must be actionable.** "HTTP 404" is useless; "Gemini does not
  recognise that model, check Settings for the list" is not.
