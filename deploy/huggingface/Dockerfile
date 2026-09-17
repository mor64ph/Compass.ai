# Compass on Hugging Face Spaces.
#
# Copy this to the ROOT of your Space repo as `Dockerfile`, alongside the
# README.md in this directory. See deploy/huggingface/README-DEPLOY.md.
#
# Differences from the root Dockerfile, all forced by the platform:
#
#   uid 1000      Spaces run the container as user 1000. A different uid cannot
#                 write anything, and the failure looks like a permissions bug
#                 in the app.
#   port 7860     The Space's `app_port`. HF routes to it.
#   no volume     Free Spaces have an ephemeral filesystem, so there is no /data
#                 mount to declare. The database lives in the container and is
#                 lost on restart - which is why COMPASS_DEMO_MODE exists.
#   no Caddy      HF terminates TLS itself, so nothing here handles it.

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

COPY requirements.txt .
# CPU-only torch. The default wheel carries CUDA libraries worth several hundred
# megabytes that a free CPU Space can never use.
RUN pip install --upgrade pip \
    && pip install --extra-index-url https://download.pytorch.org/whl/cpu \
         -r requirements.txt

# Bake the embedding model in. Downloading it on the first scoring request would
# make the first visitor wait ~90s, and a portfolio demo gets one chance.
ENV HF_HOME=/opt/hf
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
print('model cached')"

# --------------------------------------------------------------------------
FROM base AS runtime

RUN apt-get update && apt-get install --no-install-recommends -y \
        libjpeg62-turbo libfreetype6 curl \
    && rm -rf /var/lib/apt/lists/*

# Spaces run as uid 1000. Create it before copying anything, and own every
# copied path, or the app cannot write its own database.
RUN useradd -m -u 1000 user
ENV HOME=/home/user

COPY --from=builder --chown=user:user /opt/venv /opt/venv
COPY --from=builder --chown=user:user /opt/hf /opt/hf

ENV PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    COMPASS_HOST=0.0.0.0 \
    COMPASS_PORT=7860 \
    # The browser reaches HF over https, so the session cookie must be marked
    # Secure even though the internal hop from HF's proxy to this container is
    # plain http. Setting this false would leave the cookie unprotected on a
    # site that is served over TLS.
    COMPASS_HTTPS_ONLY=true \
    COMPASS_DB_URL=sqlite:////home/user/app/compass.db \
    COMPASS_PRELOAD_EMBEDDINGS=true \
    # Seed a signed-in demo account on an empty database. Without this a
    # visitor to a public Space hits an invite-only login wall and sees nothing.
    COMPASS_DEMO_MODE=true

WORKDIR /home/user/app
COPY --chown=user:user app ./app
COPY --chown=user:user scripts ./scripts
COPY --chown=user:user requirements.txt ./

RUN mkdir -p data/uploads data/exports && chown -R user:user /home/user/app

USER user
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:7860/login >/dev/null || exit 1

# One worker: the login throttle and the LLM rate limiter are both per-process,
# so a second would silently double each limit.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
