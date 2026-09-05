"""The real HTTP fetcher, exercised offline through httpx.MockTransport.

The politeness tests are not hygiene. We scrape a live public court website that owes us
nothing; an IP ban is a plausible, unrecoverable, mid-demo failure caused entirely by
us. SOURCE_MAX_CONCURRENCY and SOURCE_MIN_INTERVAL_MS are the only things preventing it.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from verifier.providers.base import Fetcher
from verifier.providers.fetcher_http import GATE, HttpFetcher, RetryableStatus
from verifier.settings import settings


@pytest.fixture(autouse=True)
def _fresh_gate():
    """The gate is process-wide by design, so tests must reset it between cases."""
    GATE._loop = None
    yield
    GATE._loop = None


def fetcher(handler, **kwargs) -> HttpFetcher:
    return HttpFetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)


def test_satisfies_the_fetcher_protocol() -> None:
    assert isinstance(HttpFetcher(), Fetcher)
    assert HttpFetcher().strategy == "http"


async def test_fetch_returns_the_body_and_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><title>[2007] SGCA 37</title></html>")

    result = await fetcher(handler).fetch("https://www.elitigation.sg/gd/s/2007_SGCA_37")
    assert result.status_code == 200
    assert "[2007] SGCA 37" in result.html
    assert result.elapsed_ms >= 0


async def test_sends_the_configured_user_agent() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, html="ok")

    await fetcher(handler).fetch("https://www.elitigation.sg/gd/s/2007_SGCA_37")
    assert seen == [settings.SOURCE_USER_AGENT]


async def test_retries_a_retryable_status_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SOURCE_MIN_INTERVAL_MS", 0)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503 if len(calls) == 1 else 200, html="ok")

    result = await fetcher(handler, max_attempts=2).fetch("https://x.test/a")
    assert result.status_code == 200
    assert len(calls) == 2


async def test_does_not_retry_a_hard_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """F3 means a missing citation comes back as 200 with a soft-404 body, so a real
    404 here is a structurally wrong request and asking again will not fix it."""
    monkeypatch.setattr(settings, "SOURCE_MIN_INTERVAL_MS", 0)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, html="")

    result = await fetcher(handler, max_attempts=3).fetch("https://x.test/a")
    assert result.status_code == 404
    assert len(calls) == 1


async def test_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SOURCE_MIN_INTERVAL_MS", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, html="")

    with pytest.raises(RetryableStatus):
        await fetcher(handler, max_attempts=2).fetch("https://x.test/a")


async def test_concurrency_never_exceeds_source_max_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SOURCE_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "SOURCE_MIN_INTERVAL_MS", 0)
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, html="ok")

    subject = fetcher(handler)
    await asyncio.gather(*(subject.fetch(f"https://x.test/{i}") for i in range(8)))
    assert peak <= 2


async def test_minimum_interval_between_request_starts_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SOURCE_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(settings, "SOURCE_MIN_INTERVAL_MS", 40)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="ok")

    subject = fetcher(handler)
    started = time.monotonic()
    for i in range(3):
        await subject.fetch(f"https://x.test/{i}")
    # Three starts at >=40ms apart: the first is free, so >=80ms total.
    assert (time.monotonic() - started) >= 0.075


async def test_the_gate_is_shared_across_fetcher_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several adapters, layers or Celery tasks constructing their own fetcher must
    still add up to one polite consumer, so the gate is module-level, not per client."""
    monkeypatch.setattr(settings, "SOURCE_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "SOURCE_MIN_INTERVAL_MS", 0)
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, html="ok")

    a, b = fetcher(handler), fetcher(handler)
    await asyncio.gather(
        *(a.fetch(f"https://x.test/a{i}") for i in range(4)),
        *(b.fetch(f"https://x.test/b{i}") for i in range(4)),
    )
    assert peak <= 2


async def test_client_is_not_constructed_at_import_time() -> None:
    """Building the transport eagerly would touch the socket module simply because a
    module was imported, which the offline test suite forbids."""
    subject = HttpFetcher()
    assert subject._client is None
