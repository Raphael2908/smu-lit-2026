"""Liveness and readiness.

``/healthz`` answers "is this process alive". ``/readyz`` answers "will a verification
actually work right now", which is a different and much more useful question.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter

from verifier.api.deps import probe_browser_session, probe_redis
from verifier.contracts.api import HealthResponse, ReadyResponse
from verifier.repos.pg import ping_database
from verifier.settings import settings

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse()


@router.get("/readyz", response_model=ReadyResponse)
async def readyz() -> ReadyResponse:
    """Report every dependency, and never fail the request.

    Deliberately 200 even when degraded: the body carries the truth, and a /readyz that
    returns 503 is a /readyz nobody reads five minutes before a demo. The
    ``browser_session`` field is the point of this endpoint -- an expired login-walled
    session degrades every login-walled citation to a WARN, which on stage is
    indistinguishable from the tool being broken.
    """
    database, redis, browser_session = await asyncio.gather(
        ping_database(),
        probe_redis(),
        probe_browser_session(),
    )

    if settings.is_mock:
        # Mock mode is self-contained by design: no database, no broker, no keys.
        status = "ok"
    else:
        status = "ok" if (database and redis) else "degraded"

    return ReadyResponse(
        status=status,
        provider_mode=settings.PROVIDER_MODE,
        database=database,
        redis=redis,
        browser_session=browser_session,
    )
