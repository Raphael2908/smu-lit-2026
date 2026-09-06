"""Headless-browser fetcher for sources with no plain-HTTP path.

TWO KINDS OF SOURCE QUALIFY, and they are different problems with one answer:

* **A login wall.** LawNet gates its corpus behind a subscription. There is no HTTP path
  at all, and no script can obtain the session.
* **A bot-detection challenge.** AGC SSO is entirely public, but sits behind an AWS WAF
  that answers httpx with ``202`` and ``x-amzn-waf-action: challenge`` and an empty body.
  A client that does not execute the page gets nothing.

The second kind is why this module's original framing -- "open sources (eLitigation, AGC
SSO) are static HTML, fetched over plain HTTP in ~0.26s" -- was only half right.
eLitigation is exactly that (F2); SSO is equally open and not fetchable that way at all.
"Open" and "reachable over HTTP" turned out to be different questions, so adapters now
declare which they need via ``SourceAdapter.fetch_strategy``.

Two rules govern this module, and both exist to avoid the same failure:

1. **We store a browser profile, not credentials.** ``make login`` opens a headed
   browser for a human to sign in once, including any SSO or 2FA, and Playwright
   persists the resulting session to a profile directory. This process never sees a
   password.

2. **An authentication failure is never a fabrication claim.** If the session has
   expired we raise ``SourceUnauthenticated``, which resolves to WARN. We could not
   check the citation; that is not evidence the citation is fake. Getting this
   backwards would report every real case as hallucinated the moment a cookie lapsed.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

from verifier.contracts.enums import FetchStrategy
from verifier.errors import RetryableError, SourceUnauthenticated
from verifier.logging import get_logger
from verifier.providers.base import FetchResult
from verifier.providers.politeness import GATE
from verifier.settings import settings

log = get_logger(__name__)

#: Markers that mean "you are not signed in" rather than "this document is missing".
#: Kept deliberately broad: a false SourceUnauthenticated costs one WARN, while
#: mistaking a login wall for a missing document costs a false fabrication claim.
_LOGIN_WALL_MARKERS = (
    "sign in",
    "log in",
    "login",
    "session has expired",
    "session expired",
    "please authenticate",
    "unauthorised access",
    "unauthorized access",
    "subscription required",
)


class BrowserFetcher:
    """Playwright + a persistent profile. Heavy, so it runs on its own Celery queue.

    Concurrency is capped hard: browsers are expensive, and this must never sit in
    front of the ~0.6s fabrication check that runs over plain HTTP.
    """

    strategy = FetchStrategy.BROWSER.value

    def __init__(self, profile_dir: str | None = None, *, user_agent: str | None = None) -> None:
        self._profile_dir = Path(profile_dir or settings.BROWSER_PROFILE_DIR).expanduser()
        self._user_agent = (
            user_agent if user_agent is not None else (settings.BROWSER_USER_AGENT or None)
        )
        self._context: Any = None
        self._playwright: Any = None
        #: The loop the context above was built on. Playwright's async objects are bound
        #: to it, this class sits behind an @lru_cache in providers.factory, and
        #: worker/tasks.py runs every Celery task under a fresh asyncio.run(). See
        #: _ensure_context.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None

    def _context_kwargs(self) -> dict[str, Any]:
        """Launch arguments, extracted so they are testable without Playwright.

        NOTE WHAT IS ABSENT: ``user_agent``, unless one was explicitly asked for. This
        used to pass ``settings.SOURCE_USER_AGENT`` unconditionally, which told a real
        Chromium to identify as ``sigma-tech/0.1``. Against the bot filter that is half
        the reason this fetcher exists, that is the worst of both worlds -- the seconds
        of a browser with none of the benefit, and a renderer/client mismatch that is
        itself a detection signal. Blank means "send whatever Chromium sends".
        """
        kwargs: dict[str, Any] = {
            "user_data_dir": str(self._profile_dir),
            "headless": settings.BROWSER_HEADLESS,
        }
        if self._user_agent:
            kwargs["user_agent"] = self._user_agent
        return kwargs

    # -- lifecycle ----------------------------------------------------------------

    async def _ensure_context(self) -> Any:
        """Launch once per event loop, reuse thereafter. Cold start is seconds.

        KEYED ON THE RUNNING LOOP, not merely on "have we launched yet". Playwright's
        async objects hold a transport bound to the loop that created them, this class
        is behind an ``@lru_cache`` in ``providers.factory``, and
        ``worker/tasks.py::_run_async`` calls ``asyncio.run()`` per Celery task -- a new
        loop each time, closed at the end, in a process that is reused. Caching a context
        across that boundary means the SECOND browser fetch in a worker dies on a closed
        loop. Same shape as todo.md bug 7, worse consequence: httpx merely holds stale
        keep-alives, whereas Playwright's objects are strictly loop-bound.
        """
        loop = asyncio.get_running_loop()
        # A Lock built on a dead loop cannot guard anything on this one.
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._context is not None and self._loop is loop:
                return self._context
            if self._context is not None:
                self._discard_stale_context()
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - depends on the image
                raise RetryableError(
                    "Playwright is not installed in this image. The browser role is built "
                    "with INSTALL_BROWSER=true; the api and worker roles are not."
                ) from exc

            self._profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                **self._context_kwargs()
            )
            self._loop = loop
            return self._context

    def _discard_stale_context(self) -> None:
        """Drop a context belonging to a loop that is gone.

        Best-effort by necessity: closing it cleanly would have to run on the loop that
        owns it, which is precisely the loop that no longer exists. Dropping the
        references is what can actually be done here; the browser process is reaped when
        the worker exits.
        """
        log.info("browser_context_rebuilt", reason="event_loop_changed")
        self._context = None
        self._playwright = None
        self._loop = None

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._loop = None

    # -- fetching -----------------------------------------------------------------

    async def fetch(self, url: str) -> FetchResult:
        started = time.perf_counter()
        context = await self._ensure_context()

        # The SHARED, process-wide gate -- the same one HttpFetcher uses. This path had a
        # private semaphore and no minimum interval at all, so the only fetcher that
        # talks to a WAF-protected government site was also the only one with no rate
        # limit on it.
        async with GATE:
            page = await context.new_page()
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=settings.BROWSER_TIMEOUT_S * 1000,
                )
                html = await page.content()
                status = response.status if response is not None else 0
            except Exception as exc:  # noqa: BLE001 - a nav failure is retryable, not a verdict
                raise RetryableError(f"Browser navigation to {url} failed: {exc}") from exc
            finally:
                await page.close()

        if _looks_like_login_wall(html):
            # WARN, not FAIL. See the module docstring.
            raise SourceUnauthenticated(
                f"{url} returned a sign-in page. The stored browser session has expired -- "
                "run 'make login' to re-authenticate. This citation is UNVERIFIED, "
                "which is not the same as fabricated."
            )

        return FetchResult(
            url=url,
            status_code=status,
            html=html,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            authenticated=True,
        )

    async def healthy(self) -> bool:
        """Reported by /readyz so an expired session is visible BEFORE a demo."""
        try:
            context = await self._ensure_context()
        except Exception:  # noqa: BLE001 - health checks report, they do not raise
            return False
        return context is not None and self._profile_dir.exists()


def _looks_like_login_wall(html: str) -> bool:
    """Only the first ~4KB is scanned: a sign-in wall announces itself immediately,
    whereas a real judgment can mention 'log in' deep in unrelated body text."""
    head = html[:4000].lower()
    return any(marker in head for marker in _LOGIN_WALL_MARKERS)


async def _interactive_login(url: str) -> None:
    """`make login`: a headed browser for a one-time human sign-in.

    Deliberately manual. Automating a credential flow would mean this system holding a
    password for a subscription service, and the profile directory achieves the same
    result without ever seeing one.
    """
    from playwright.async_api import async_playwright

    profile = Path(settings.BROWSER_PROFILE_DIR).expanduser()
    profile.mkdir(parents=True, exist_ok=True)

    print(f"\nOpening a browser. Sign in, then close the window.\nProfile: {profile}\n")
    async with async_playwright() as pw:
        # Same UA source as BrowserFetcher, deliberately. A session established under one
        # user agent and used under another is one some sites will invalidate.
        kwargs: dict[str, Any] = {"user_data_dir": str(profile), "headless": False}
        if settings.BROWSER_USER_AGENT:
            kwargs["user_agent"] = settings.BROWSER_USER_AGENT
        context = await pw.chromium.launch_persistent_context(**kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url)
        print("Waiting for you to finish and close the browser...")
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:  # noqa: BLE001 - the user closing the window is the success path
            pass
        await context.close()
    print(f"\nSession saved to {profile}. It persists across restarts.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser fetcher maintenance.")
    parser.add_argument("--login", action="store_true", help="Headed one-time sign-in.")
    parser.add_argument("--url", default="https://www.lawnet.sg/", help="URL to sign in at.")
    args = parser.parse_args()
    if args.login:
        asyncio.run(_interactive_login(args.url))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
