"""Source politeness: one process-wide gate, shared by every fetcher.

Split out of ``fetcher_http`` so the BROWSER fetcher can use it too. It could not
before, and the consequence was concrete: ``BrowserFetcher`` had a per-instance
semaphore and NO minimum interval at all, so the only path that talks to a WAF-protected
government site was also the only path with no rate limit on it.

The gate is module-level on purpose. Several adapters, layers or Celery tasks each
constructing their own fetcher must still add up to one polite consumer of a source.
"""

from __future__ import annotations

import asyncio
import time

from verifier.settings import settings

__all__ = ["GATE"]


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
