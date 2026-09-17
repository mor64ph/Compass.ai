"""Compass - Phase 1 application entry point.

    uvicorn app.main:app --reload

Phase 1 is single-user and local: SQLite on disk, no auth, bound to localhost.
Multi-tenancy, encryption at rest and a real privacy policy are Phase 3
concerns (PRD Section 9) and are deliberately absent rather than half-built.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import PROJECT_ROOT, get_settings
from app.db import init_db
from app.routers import (
    applications,
    auth_router,
    dashboard,
    prep,
    profile,
    resumes,
    settings_router,
    tracker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# Third-party chatter that buries Compass's own log lines. The embedding model
# revalidates its cache against Hugging Face on load, which alone emits a dozen
# INFO lines per start. Set HF_HUB_OFFLINE=1 to skip that entirely once the
# model is cached.
for _noisy in ("httpx", "httpcore", "urllib3", "filelock", "sentence_transformers"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("compass")

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Before any outbound HTTPS: on a managed machine a TLS-inspecting proxy
    # re-signs traffic with an internal CA that Windows trusts and Python does
    # not. See app/tls.py.
    from app.tls import use_system_certificates

    use_system_certificates()

    init_db()
    if settings.compass_secret_key == "dev-only-change-me":
        logger.warning(
            "COMPASS_SECRET_KEY is still the default. Set it in .env before "
            "using the Google integration."
        )
    if settings.compass_preload_embeddings:
        # Daemon thread: startup is not blocked, but the ~45s sentence-transformer
        # load happens now rather than on the user's first scoring request.
        from app.services import embeddings

        embeddings.preload_in_background()
    # Deliberately does not print a URL: uvicorn already logs the address it
    # actually bound, and echoing the *configured* host/port here contradicts it
    # whenever the port is overridden on the command line.
    logger.info("Compass startup complete (database: %s)", settings.compass_db_url)
    yield


app = FastAPI(
    title="Compass",
    description="A personal, AI-native job search copilot. Phase 1.",
    version="0.1.0",
    # No public API surface is intended; the docs pages are just noise here.
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.compass_secret_key,
    same_site="lax",
    https_only=False,  # local http only
)

static_dir = PROJECT_ROOT / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth_router.router)  # the only unauthenticated routes
app.include_router(dashboard.router)
app.include_router(profile.router)
app.include_router(resumes.router)
app.include_router(applications.router)
app.include_router(tracker.router)
app.include_router(prep.router)
app.include_router(settings_router.router)


from app.web import templates as _tpl


def _err(request: Request, status: int, title: str, detail: str):
    return _tpl.TemplateResponse(
        request,
        "error.html",
        {"request": request, "flashes": [], "user": None,
         "status_code": status, "title": title, "detail": detail},
        status_code=status,
    )


@app.exception_handler(404)
async def _404(request: Request, exc):
    return _err(request, 404, "Page not found",
                "The page you're looking for doesn't exist or you don't have access to it.")


@app.exception_handler(403)
async def _403(request: Request, exc):
    return _err(request, 403, "Access denied",
                "You don't have permission to view this page. Try signing in.")


@app.exception_handler(422)
async def _422(request: Request, exc):
    return _err(request, 422, "Invalid request",
                "The URL or form contained invalid data. Go back and try again.")


@app.exception_handler(500)
async def _500(request: Request, exc):
    logger.exception("Unhandled 500: %s", exc)
    return _err(request, 500, "Something went wrong",
                "An unexpected error occurred. Try refreshing — if it keeps happening, restart the server.")


def run() -> None:
    """`python -m app.main` convenience entry point."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.compass_host,
        port=settings.compass_port,
        reload=True,
    )


if __name__ == "__main__":
    run()
