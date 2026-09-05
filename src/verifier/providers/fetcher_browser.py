"""Headless-browser fetcher for login-walled sources.

Open sources (eLitigation, AGC SSO) are static HTML and are fetched over plain HTTP in
~0.26s -- a browser there would add seconds for nothing. This exists for the sources
that a subscription gates, LawNet chief among them, where there is no HTTP path at all.

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

    def __init__(self, profile_dir: str | None = None) -> None:
        self._profile_dir = Path(profile_dir or settings.BROWSER_PROFILE_DIR).expanduser()
        self._context: Any = None
        self._playwright: Any = None
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, settings.SOURCE_MAX_CONCURRENCY))

    # -- lifecycle ----------------------------------------------------------------

    async def _ensure_context(self) -> Any:
        """Launch once, reuse thereafter. Cold start is seconds; do not pay it twice."""
        async with self._lock:
            if self._context is not None:
                return self._context
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
                user_data_dir=str(self._profile_dir),
                headless=settings.BROWSER_HEADLESS,
                user_agent=settings.SOURCE_USER_AGENT,
            )
            return self._context

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    # -- fetching -----------------------------------------------------------------

    async def fetch(self, url: str) -> FetchResult:
        started = time.perf_counter()
        context = await self._ensure_context()

        async with self._semaphore:
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
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, user_agent=settings.SOURCE_USER_AGENT
        )
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
