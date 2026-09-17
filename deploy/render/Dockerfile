# Compass on Render's free tier.
#
# Build context is the repository root:
#   docker build -f deploy/render/Dockerfile .
# which is what render.yaml tells Render to do.
#
# Differences from the root Dockerfile, all forced by 512 MB of RAM:
#
#   slim deps   No sentence-transformers, so no torch. See requirements-slim.txt
#               for exactly what that costs — it is not free.
#   no model    Nothing to bake in; the lexical backend needs no weights, which
#               also makes the image small and the build fast.
#   $PORT       Render assigns the port at runtime and the container must bind
#               whatever it is given. A hardcoded port fails health checks.
#   no volume   Free services have no persistent disk, so the database is
#               rebuilt on every deploy and every wake from idle. That is what
#               COMPASS_DEMO_MODE is for.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --------------------------------------------------------------------------
FROM base AS builder

RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential libjpeg-dev zlib1g-dev libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY deploy/render/requirements-slim.txt ./requirements-slim.txt
RUN pip install --upgrade pip && pip install -r requirements-slim.txt

# --------------------------------------------------------------------------
FROM base AS runtime

RUN apt-get update && apt-get install --no-install-recommends -y \
        libjpeg62-turbo libfreetype6 curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 compass

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    COMPASS_HOST=0.0.0.0 \
    # Render terminates TLS, so the browser reaches this over https and the
    # session cookie must be marked Secure even though the internal hop is http.
    COMPASS_HTTPS_ONLY=true \
    COMPASS_DB_URL=sqlite:////home/compass/app/compass.db \
    # Nothing to preload — there is no model. Leaving this true would spend a
    # background thread discovering that on every boot.
    COMPASS_PRELOAD_EMBEDDINGS=false \
    # Seeds the published demo account on the empty database this service
    # always starts with. Without it a visitor meets the invite-only wall.
    COMPASS_DEMO_MODE=true

WORKDIR /home/compass/app
COPY --chown=compass:compass app ./app
COPY --chown=compass:compass scripts ./scripts

RUN mkdir -p data/uploads data/exports && chown -R compass:compass /home/compass/app

USER compass

# Shell form on purpose: $PORT has to be expanded at runtime by the shell, and
# exec form would pass the literal string "$PORT" to uvicorn.
#
# One worker — the login throttle and the LLM rate limiter are per-process, so a
# second would silently double both. On 0.1 CPU it is also all there is room for.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} \
      --workers 1 --proxy-headers --forwarded-allow-ips '*'
