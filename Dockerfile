# Compass - production image.
#
# Targets linux/arm64 (Oracle Cloud's Always Free Ampere instances) but builds
# unchanged on amd64. See docs/DEPLOY.md.
#
# Two decisions worth knowing about:
#
#  1. CPU-only torch, from PyTorch's cpu index. The default wheel drags in CUDA
#     libraries that are several hundred megabytes and cannot be used on a
#     machine with no NVIDIA GPU - which is every instance this will run on.
#
#  2. The sentence-transformer model is baked in at build time. Left to runtime
#     it downloads ~90 MB on the first scoring request, which would make the
#     first user wait and make the container dependent on Hugging Face being
#     reachable. Baked in, `local_files_only=True` finds it immediately and the
#     container works with no outbound network at all.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --------------------------------------------------------------------------
# Builder: compile wheels, fetch the model
# --------------------------------------------------------------------------
FROM base AS builder

# Build-only toolchain. pdfplumber/pillow and friends need a compiler on arm64
# where prebuilt wheels are patchier than on amd64.
RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        libjpeg-dev \
        zlib1g-dev \
        libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --extra-index-url https://download.pytorch.org/whl/cpu \
         -r requirements.txt

# Pre-download the embedding model into the image.
ENV HF_HOME=/opt/hf
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2'); \
print('model cached')"

# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------
FROM base AS runtime

# Runtime libs only - no compiler. `curl` is here for the container healthcheck.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libjpeg62-turbo \
        libfreetype6 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged. The uid is pinned so the bind-mounted volume's ownership can be
# set from the host without guessing.
RUN useradd --create-home --uid 10001 compass

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=compass:compass /opt/hf /opt/hf

ENV PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf \
    # The model is already local, so skip the Hugging Face revalidation that
    # otherwise costs ~50s on every process start.
    HF_HUB_OFFLINE=1 \
    # Bind every interface: the container's own network, not the public one.
    # Caddy in front is what the internet actually reaches.
    COMPASS_HOST=0.0.0.0 \
    COMPASS_PORT=8000 \
    # Caddy terminates TLS, so the cookie must be marked Secure.
    COMPASS_HTTPS_ONLY=true \
    # On the mounted volume, so the database survives a redeploy. Four slashes:
    # sqlite:/// plus an absolute /data path.
    COMPASS_DB_URL=sqlite:////data/compass.db \
    COMPASS_PRELOAD_EMBEDDINGS=true

WORKDIR /app
COPY --chown=compass:compass app ./app
COPY --chown=compass:compass scripts ./scripts
COPY --chown=compass:compass requirements.txt README.md ./

# Writable state. Both are volume mount points in compose; creating them here
# means the image also runs standalone for a smoke test.
RUN mkdir -p /data /app/data/uploads /app/data/exports \
    && chown -R compass:compass /data /app/data

USER compass
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8000/login >/dev/null || exit 1

# One worker, deliberately. The login throttle and the LLM rate limiter are
# per-process, so a second worker would silently double both. This is a tool for
# a handful of invited people; concurrency is not the constraint, and SQLite
# would be the next thing to complain anyway.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
