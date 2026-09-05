"""The offline fetcher. Every assertion here also asserts 'no network'."""

from __future__ import annotations

import pytest

from verifier.providers.base import Fetcher
from verifier.providers.mock.fetcher import MockFetcher

BASE = "https://www.elitigation.sg"


def test_satisfies_the_fetcher_protocol() -> None:
    assert isinstance(MockFetcher(), Fetcher)


def test_strategy_kwarg_is_accepted_and_reported() -> None:
    """providers/factory.py builds the browser-mode mock as MockFetcher(strategy=...)."""
    assert MockFetcher().strategy == "http"
    assert MockFetcher(strategy="browser").strategy == "browser"


CORPUS_CASES = [
    (f"{BASE}/gd/s/2007_SGCA_37", 200, "[2007] SGCA 37"),
    (f"{BASE}/gd/s/2021_SGHC_100", 200, "[2021] SGHC 100"),
]


@pytest.mark.parametrize(("url", "status", "marker"), CORPUS_CASES)
async def test_known_citations_serve_the_real_corpus(url: str, status: int, marker: str) -> None:
    result = await MockFetcher().fetch(url)
    assert result.status_code == status
    assert f"<title>{marker}</title>" in result.html


async def test_unknown_citation_serves_the_soft_404_with_status_200() -> None:
    """F3: a fabricated citation returns HTTP 200. A mock that returned 404 would hide
    the entire problem this system exists to solve."""
    result = await MockFetcher().fetch(f"{BASE}/gd/s/2019_SGCA_999")
    assert result.status_code == 200
    assert "Page Not Found" in result.html
    assert len(result.html) < 10_000


async def test_maintenance_token_forces_the_third_page_state() -> None:
    """The maintenance page cannot be summoned from the live site on demand, and it is
    the state whose mishandling reports every real case as fabricated."""
    result = await MockFetcher().fetch(f"{BASE}/gd/s/__maintenance__")
    assert "Maintenance Notice" in result.html


async def test_search_returns_hits_for_a_known_phrase_and_none_for_a_fake_one() -> None:
    fetcher = MockFetcher()
    real = await fetcher.fetch(f"{BASE}/gd/Home/Index?SearchPhrase=Spandeck+Engineering")
    fake = await fetcher.fetch(f"{BASE}/gd/Home/Index?SearchPhrase=Kaya+Toast+Investments")
    assert "/gd/s/2007_SGCA_37" in real.html
    assert "/gd/s/" not in fake.html


async def test_calls_are_recorded_for_assertions() -> None:
    fetcher = MockFetcher()
    await fetcher.fetch(f"{BASE}/gd/s/2007_SGCA_37")
    assert fetcher.calls == [f"{BASE}/gd/s/2007_SGCA_37"]


async def test_unknown_path_is_a_404() -> None:
    assert (await MockFetcher().fetch(f"{BASE}/nope")).status_code == 404


# -- SSO --------------------------------------------------------------------------


async def test_an_sso_url_is_dispatched_on_its_host_not_its_path() -> None:
    """Path-only dispatch was fine with one source. With two it would serve eLitigation
    judgment fixtures for any SSO URL that happened to contain /gd/s/."""
    result = await MockFetcher().fetch("https://sso.agc.gov.sg/gd/s/2007_SGCA_37")
    assert result.status_code == 200
    assert "Spandeck" not in result.html
    assert "Synthetic SSO document" in result.html


async def test_an_sso_legislation_url_serves_a_synthetic_page() -> None:
    result = await MockFetcher(strategy="browser").fetch("https://sso.agc.gov.sg/Act/IA1959")
    assert result.status_code == 200
    assert "legisContent" in result.html


def test_there_is_no_synthetic_sso_soft_404_fixture() -> None:
    """Deliberate. SSO's soft-404 has never been observed through the path the adapter
    uses, so an invented fixture would let a test establish a NOT_FOUND branch that no
    measurement supports -- which is precisely how a fabrication verdict gets built on an
    assumption. Add one in the same commit as scripts/sso_probe.py's results."""
    from verifier.providers.mock import fetcher as mock_fetcher

    source = (mock_fetcher.__file__ or "").replace(".pyc", ".py")
    text = open(source, encoding="utf-8").read()
    assert "SSO_SOFT_404" not in text
    assert "sso_not_found" not in text
