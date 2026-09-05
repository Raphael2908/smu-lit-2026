"""The browser fetcher's job is to fail in the right direction.

Everything Playwright-dependent is out of reach offline, so these tests pin the one
piece of logic that decides a verdict: whether a page is a login wall. Getting that
backwards would report every real case as fabricated the moment a session cookie
lapsed, which is the single most damaging failure this system can have.
"""

from __future__ import annotations

import pytest

from verifier.errors import SourceUnauthenticated
from verifier.providers.fetcher_browser import BrowserFetcher, _looks_like_login_wall

LOGIN_WALLS = [
    "<html><body><h1>Sign in</h1><form>...</form></body></html>",
    "<html><body>Your session has expired. Please log in again.</body></html>",
    "<html><body>Subscription required to view this document.</body></html>",
    "<html><body>Unauthorised access. Please authenticate.</body></html>",
]

REAL_PAGES = [
    "<title>[2007] SGCA 37</title><p>The appellant appealed against the decision...</p>",
    "<title>[2021] SGHC 100</title><p>This is an application for summary judgment.</p>",
]


@pytest.mark.parametrize("html", LOGIN_WALLS)
def test_a_login_wall_is_recognised(html):
    assert _looks_like_login_wall(html) is True


@pytest.mark.parametrize("html", REAL_PAGES)
def test_a_judgment_is_not_mistaken_for_a_login_wall(html):
    assert _looks_like_login_wall(html) is False


def test_only_the_head_of_the_page_is_scanned():
    """A judgment may discuss a 'log in' requirement in its own facts.

    A sign-in wall announces itself immediately; body text 4KB deep does not. Scanning
    the whole document would turn any judgment about, say, a computer-misuse offence
    into an authentication failure.
    """
    judgment = "<title>[2019] SGHC 12</title>" + ("<p>The respondent's evidence. </p>" * 400)
    judgment += "<p>The accused was required to log in using the victim's password.</p>"
    assert len(judgment) > 8000
    assert _looks_like_login_wall(judgment) is False


def test_source_unauthenticated_is_not_a_provider_fatal_error():
    """It must resolve to WARN, never FAIL.

    Being unable to check a citation is not evidence that the citation is fake. If
    SourceUnauthenticated ever became a FatalError it would start failing runs, so the
    hierarchy is asserted rather than assumed.
    """
    from verifier.errors import FatalError, ProviderError

    assert issubclass(SourceUnauthenticated, ProviderError)
    assert not issubclass(SourceUnauthenticated, FatalError)


def test_the_fetcher_declares_the_browser_strategy():
    assert BrowserFetcher.strategy == "browser"


def test_a_profile_directory_is_used_rather_than_credentials():
    """We persist a browser profile, never a password.

    ``make login`` opens a headed browser for a human to sign in once -- SSO and 2FA
    included -- and this process never sees the credential.
    """
    fetcher = BrowserFetcher(profile_dir="/tmp/does-not-exist-profile")
    assert str(fetcher._profile_dir).endswith("does-not-exist-profile")


# -- user agent -------------------------------------------------------------------
#
# This used to pass settings.SOURCE_USER_AGENT unconditionally, telling a real Chromium
# to identify as "sal-verifier/0.1". Against the bot filter that is half the reason this
# fetcher exists, that is the worst of both worlds: the seconds of a browser with none of
# the benefit, plus a renderer/client mismatch that is itself a detection signal.


def test_no_user_agent_is_sent_by_default() -> None:
    kwargs = BrowserFetcher(profile_dir="/tmp/does-not-matter")._context_kwargs()
    assert "user_agent" not in kwargs
    assert kwargs["user_data_dir"] == "/tmp/does-not-matter"


def test_an_explicit_user_agent_is_passed_through() -> None:
    fetcher = BrowserFetcher(profile_dir="/tmp/x", user_agent="Mozilla/5.0 (compatible; x)")
    assert fetcher._context_kwargs()["user_agent"] == "Mozilla/5.0 (compatible; x)"


def test_the_settings_user_agent_is_used_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from verifier.settings import settings

    monkeypatch.setattr(settings, "BROWSER_USER_AGENT", "Mozilla/5.0 (compatible; sal)")
    assert BrowserFetcher()._context_kwargs()["user_agent"] == "Mozilla/5.0 (compatible; sal)"


def test_the_source_user_agent_is_not_reused_for_the_browser() -> None:
    """The regression guard. SOURCE_USER_AGENT is the string SSO answers with 403."""
    from verifier.settings import settings

    assert BrowserFetcher()._context_kwargs().get("user_agent") != settings.SOURCE_USER_AGENT


# -- event-loop binding -----------------------------------------------------------


def test_a_context_from_a_dead_loop_is_discarded() -> None:
    """Playwright's objects are bound to the loop that created them, this class sits
    behind an lru_cache, and worker/tasks.py runs asyncio.run() per Celery task. Without
    this, the SECOND browser fetch in a worker dies on a closed loop."""
    fetcher = BrowserFetcher()
    fetcher._context = object()
    fetcher._playwright = object()
    fetcher._loop = object()  # type: ignore[assignment]

    fetcher._discard_stale_context()

    assert fetcher._context is None
    assert fetcher._playwright is None
    assert fetcher._loop is None


async def test_a_cached_context_is_reused_within_one_loop() -> None:
    import asyncio

    fetcher = BrowserFetcher()
    sentinel = object()
    fetcher._context = sentinel
    fetcher._loop = asyncio.get_running_loop()

    assert await fetcher._ensure_context() is sentinel


def test_browser_fetches_go_through_the_shared_politeness_gate() -> None:
    """It had a private semaphore and no minimum interval at all, so the one fetcher that
    talks to a WAF-protected government site was the one with no rate limit."""
    import inspect

    from verifier.providers import fetcher_browser

    assert "async with GATE" in inspect.getsource(fetcher_browser.BrowserFetcher.fetch)
    assert fetcher_browser.GATE is not None
