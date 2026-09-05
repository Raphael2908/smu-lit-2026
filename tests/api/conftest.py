"""API test fixtures.

The whole suite runs offline: ``TestClient`` speaks to the app over an in-process ASGI
transport, so pytest-socket never sees a socket. Nothing here reaches Postgres, Redis
or a vendor -- if a test could, that is a bug in the fixtures.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

SAMPLE = {
    "question": "What is the test for a duty of care in Singapore?",
    "ai_output": (
        "The Court of Appeal set out a single two-stage test in Spandeck Engineering "
        "(S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37."
    ),
}


@pytest.fixture(autouse=True)
def _fresh_app_state():
    """In-memory repos ARE the store, so leaking one between tests leaks runs."""
    from verifier.api.deps import reset_state

    reset_state()
    yield
    reset_state()


@pytest.fixture
def no_dispatch(monkeypatch):
    """Stop the background pipeline dispatch.

    Most API tests are about the transport, not the pipeline, and the orchestrator is
    another stream's concern. Suppressing dispatch keeps a run parked in ``pending`` so
    assertions are deterministic; ``test_dispatch.py`` exercises the real path.
    """
    from verifier.api import deps

    # One patch point: routes call ``deps.enqueue_run`` through the module, so this is
    # the only reference there is to swap.
    monkeypatch.setattr(deps, "enqueue_run", lambda run_id: None)


@pytest.fixture
def client(no_dispatch) -> Iterator[TestClient]:
    from verifier.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def live_client(no_dispatch) -> Iterator[TestClient]:
    """Sync client used by tests that drive ``_run_inline`` themselves."""
    from verifier.api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
async def async_client() -> AsyncIterator[httpx.AsyncClient]:
    """An in-process ASGI client that SHARES THE TEST'S EVENT LOOP.

    The synchronous ``TestClient`` runs the app on a portal thread and blocks the
    caller; inside an ``async def`` test that starves the fire-and-forget dispatch task
    the whole point of ``POST /v1/verify`` is to schedule. Anything that has to observe
    background work must use this fixture. It also never opens a socket, so
    pytest-socket stays satisfied.
    """
    from verifier.api.app import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://verifier.test") as client:
        yield client
    from verifier.api.deps import cancel_background

    await cancel_background()
