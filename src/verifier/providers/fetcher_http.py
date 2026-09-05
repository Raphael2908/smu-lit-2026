"""Plain-HTTP fetcher for open sources.

F2 says a judgment is static HTML -- 115k characters in 0.26s over plain HTTP, no JS, no
browser. So the fast path is httpx and the browser stays off it entirely.

The politeness gate is not optional decoration. We are scraping a live public court
website that owes us nothing. An IP ban is a plausible, unrecoverable, mid-demo failure
mode, and it is caused by us, so ``SOURCE_MAX_CONCURRENCY`` (2) and
``SOURCE_MIN_INTERVAL_MS`` (250) are enforced PROCESS-WIDE rather than per client:
several adapters, layers or Celery tasks constructing their own fetcher must still add
up to one polite consumer.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from verifier.providers.base import FetchResult
from verifier.settings import settings

#: Status codes worth retrying. 4xx (other than 429) is not: eLitigation answers a
#: fabricated citation with 200 and a soft-404 body (F3), so a genuine 4xx here means we
#: asked for something structurally wrong and asking again will not fix it.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class RetryableStatus(Exception):
    """Raised internally so tenacity can drive the retry on a bad status code."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"retryable status {status_code}")
        self.status_code = status_code


class _PolitenessGate:
    """Process-wide concurrency cap plus a minimum interval between request starts.

    The asyncio primitives are created lazily and rebuilt if the running event loop
    changes, because a Semaphore built on one loop is silently useless on another --
    which in practice means the rate limit vanishes exactly when a worker process starts
    a fresh loop.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._lock: asyncio.Lock | None = None
        self._last_start: float = 0.0

    def _ensure(self) -> tuple[asyncio.Semaphore, asyncio.Lock]:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._semaphore is None or self._lock is None:
            self._loop = loop
            self._semaphore = asyncio.Semaphore(max(1, settings.SOURCE_MAX_CONCURRENCY))
            self._lock = asyncio.Lock()
            self._last_start = 0.0
        return self._semaphore, self._lock

    async def __aenter__(self) -> _PolitenessGate:
        semaphore, lock = self._ensure()
        await semaphore.acquire()
        try:
            interval = settings.SOURCE_MIN_INTERVAL_MS / 1000.0
            async with lock:
                wait = self._last_start + interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_start = time.monotonic()
        except BaseException:
            semaphore.release()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._semaphore is not None:
            self._semaphore.release()


#: One gate for the whole process. Deliberately module-level.
GATE = _PolitenessGate()


class HttpFetcher:
    """``Fetcher`` implementation over ``httpx.AsyncClient``."""

    strategy = "http"

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        timeout: float | None = None,
        max_attempts: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._user_agent = user_agent or settings.SOURCE_USER_AGENT
        self._timeout = timeout if timeout is not None else settings.SOURCE_TIMEOUT_S
        self._max_attempts = (
            max_attempts if max_attempts is not None else settings.TASK_MAX_RETRIES + 1
        )
        self._client = client
        self._owns_client = client is None

    @property
    def headers(self) -> dict[str, str]:
        """Sent on every request, not just set on the client we happen to build.

        Identifying ourselves to a public court site is half of polite scraping, and an
        injected or reused client must not be able to silently drop it.
        """
        return {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-SG,en;q=0.9",
        }

    def _ensure_client(self) -> httpx.AsyncClient:
        """Build the client on first use, never at import time.

        Constructing it eagerly would pin a transport (and, in tests, touch the socket
        module) simply because a module was imported.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers=self.headers,
            )
        return self._client

    async def fetch(self, url: str) -> FetchResult:
        client = self._ensure_client()
        started = time.perf_counter()
        response: httpx.Response | None = None

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatus)),
            reraise=True,
        ):
            with attempt:
                async with GATE:
                    response = await client.get(url, headers=self.headers)
                if response.status_code in RETRYABLE_STATUS:
                    raise RetryableStatus(response.status_code)

        assert response is not None  # noqa: S101 - reraise=True guarantees this
        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            html=response.text,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    async def healthy(self) -> bool:
        try:
            result = await self.fetch(settings.ELITIGATION_BASE_URL)
        except Exception:
            return False
        return result.status_code < 500

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
