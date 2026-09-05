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
