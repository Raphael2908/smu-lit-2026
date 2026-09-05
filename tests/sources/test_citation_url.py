"""Neutral citation -> URL, and back. F1 and F10."""

from __future__ import annotations

import pytest

from verifier.contracts.citations import ExtractedCitation, Span
from verifier.contracts.enums import CitationType
from verifier.sources.elitigation.citation_url import (
    build_url,
    citation_from_url,
    citation_slug,
    is_judgment_url,
    normalise_court,
)

BASE = "https://www.elitigation.sg"

SLUG_CASES = [
    ("[2007] SGCA 37", "2007_SGCA_37"),
    ("[2021] SGHC 100", "2021_SGHC_100"),
    ("[2020] SGHC(A) 4", "2020_SGHCA_4"),
    ("[2023] SGHC(I) 9", "2023_SGHCI_9"),
    ("[2022] SGCA(I) 1", "2022_SGCAI_1"),
    ("[2019] SGHCF 12", "2019_SGHCF_12"),
]


@pytest.mark.parametrize(("citation", "slug"), SLUG_CASES)
def test_build_url_from_string(citation: str, slug: str) -> None:
    assert build_url(citation, base_url=BASE) == f"{BASE}/gd/s/{slug}"


@pytest.mark.parametrize(
    ("court", "expected"),
    [("SGHC(A)", "SGHCA"), ("SGCA(I)", "SGCAI"), ("SGHC(I)", "SGHCI"), ("SGCA", "SGCA")],
)
def test_parenthesised_courts_are_stripped_not_encoded(court: str, expected: str) -> None:
    """F10. %28A%29 returns a soft-404, and a soft-404 is HTTP 200 (F3) -- so encoding
    instead of stripping does not raise, it reports every Appellate Division case as
    fabricated."""
    assert normalise_court(court) == expected
    assert "%" not in citation_slug(2020, court, 4)


def test_build_url_from_extracted_citation() -> None:
    citation = ExtractedCitation(
        ordinal=0,
        raw_text="[2007] SGCA 37",
        citation_type=CitationType.NEUTRAL,
        span=Span(start=0, end=14),
        court="SGCA",
        year=2007,
        number=37,
    )
    assert build_url(citation, base_url=BASE) == f"{BASE}/gd/s/2007_SGCA_37"


@pytest.mark.parametrize("citation", ["[2007] 4 SLR(R) 100", "Spandeck v DSTA", "nonsense"])
def test_non_neutral_citations_have_no_url(citation: str) -> None:
    """Returning None rather than guessing keeps report citations (F7) out of the fetch
    path entirely: they are unresolvable by construction, not missing."""
    assert build_url(citation, base_url=BASE) is None


def test_report_citation_object_has_no_url() -> None:
    citation = ExtractedCitation(
        ordinal=0,
        raw_text="[2007] 4 SLR(R) 100",
        citation_type=CitationType.REPORT,
        span=Span(start=0, end=19),
        year=2007,
    )
    assert build_url(citation, base_url=BASE) is None


ROUNDTRIP = [
    (f"{BASE}/gd/s/2007_SGCA_37", "[2007] SGCA 37"),
    ("/gd/s/2021_SGHC_100", "[2021] SGHC 100"),
    (f"{BASE}/gd/s/2020_SGHCA_4", "[2020] SGHC(A) 4"),
    (f"{BASE}/gd/s/2022_SGCAI_1", "[2022] SGCA(I) 1"),
]


@pytest.mark.parametrize(("url", "citation"), ROUNDTRIP)
def test_citation_from_url(url: str, citation: str) -> None:
    """The parser needs to know what citation a page was SUPPOSED to be: the three-state
    classifier turns on whether the <title> equals the citation we asked for."""
    assert citation_from_url(url) == citation


@pytest.mark.parametrize(
    "url", [f"{BASE}/gd/Home/Index?SearchPhrase=x", f"{BASE}/", "https://example.com/a"]
)
def test_non_judgment_urls(url: str) -> None:
    assert citation_from_url(url) is None
    assert not is_judgment_url(url)
