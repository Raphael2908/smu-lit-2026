"""FastAPI application factory.

Hard requirement, and the thing most of this file exists for: **the app must import and
boot with an empty ``.env``, in ``PROVIDER_MODE=mock``, with no database and no broker
reachable.** Nothing here connects to anything at import time or during startup; every
dependency is probed lazily and reported through ``/readyz``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from verifier.api.deps import cancel_background
from verifier.api.routes import health, lists, runs, verify
from verifier.errors import VerifierError
from verifier.logging import configure_logging, get_logger, run_id_var
from verifier.settings import settings

log = get_logger("verifier.api")

REQUEST_ID_HEADER = "X-Request-ID"


def _origin_regex() -> str:
    """CORS for a Chrome extension is not just ``chrome-extension://*``.

    The panel's fetches come from a CONTENT SCRIPT, and a content script's requests
    carry the *page's* origin -- ``https://claude.ai`` -- not the extension's. Allowing
    only ``chrome-extension://`` produces a demo where every request is blocked by CORS
    with nothing obviously wrong in the code. Both origins are allowed, plus localhost
    for curl and any dev UI.
    """
    patterns = [
        r"chrome-extension://[a-p]{32}",
        r"chrome-extension://.*",
        r"moz-extension://.*",
        r"https://claude\.ai",
        r"https://[a-z0-9-]+\.claude\.ai",
        r"http://localhost(:\d+)?",
        r"http://127\.0\.0\.1(:\d+)?",
    ]
    for origin in settings.cors_origins_list:
        # Configured origins may contain a wildcard; translate it to a regex fragment.
        patterns.append(re.escape(origin).replace(r"\*", ".*"))
    return "|".join(f"({p})" for p in patterns)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.LOG_LEVEL)
    log.info("api_start", provider_mode=settings.PROVIDER_MODE, env=settings.ENV)
    try:
        yield
    finally:
        await cancel_background()
        if not settings.is_mock:
            from verifier.repos.session import dispose_engine

            await dispose_engine()
        log.info("api_stop")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sigma Tech",
        version="0.1.0",
        description=(
            "Four-layer verification of legal AI outputs. Layers 1-3 are deterministic "
            "and run first; if any fails the judge is never consulted."
        ),
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_origin_regex=_origin_regex(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """Correlate a request across the API, the pipeline and the logs.

        The extension can send its own id; otherwise one is minted. It is bound into
        structlog's contextvars so every log line for this request carries it without
        anything having to thread it through by hand.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        token = run_id_var.set(None)
        try:
            response = await call_next(request)
        finally:
            run_id_var.reset(token)
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(VerifierError)
    async def verifier_error_handler(request: Request, exc: VerifierError) -> JSONResponse:
        log.warning("verifier_error", error=str(exc), type=type(exc).__name__)
        return JSONResponse(
            status_code=500, content={"error": type(exc).__name__, "detail": str(exc)}
        )

    app.include_router(health.router)
    app.include_router(verify.router, prefix="/v1")
    app.include_router(runs.router, prefix="/v1")
    app.include_router(lists.router, prefix="/v1")
    return app


app = create_app()
