"""The three-state page classifier, and structured judgment extraction (F3, F5, F12).

The classification tests are the most important in this repo. Getting them wrong does
not produce a crash or a missing feature -- it produces a confident, wrong accusation of
fabrication against every real Singapore case, for as long as eLitigation is down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from verifier.contracts.enums import ChunkKind
from verifier.sources.elitigation.parser import PageState, classify, parse, parse_document

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
BASE = "https://www.elitigation.sg"

SPANDECK_URL = f"{BASE}/gd/s/2007_SGCA_37"
NG_URL = f"{BASE}/gd/s/2021_SGHC_100"
FAKE_URL = f"{BASE}/gd/s/2019_SGCA_999"


def read(name: str) -> str:
    return (CORPUS / name).read_text(encoding="utf-8")


# --- F12: three states, one HTTP status code -------------------------------

CLASSIFICATION_CASES = [
    ("2007_SGCA_37.html", SPANDECK_URL, PageState.JUDGMENT, "[2007] SGCA 37"),
    ("2021_SGHC_100.html", NG_URL, PageState.JUDGMENT, "[2021] SGHC 100"),
    ("soft404_2019_SGCA_999.html", FAKE_URL, PageState.NOT_FOUND, ""),
    (
        "maintenance_notice.html",
        SPANDECK_URL,
        PageState.UNAVAILABLE,
        ":: eLitigation - Maintenance Notice ::",
    ),
]


@pytest.mark.parametrize(("fixture", "url", "state", "title"), CLASSIFICATION_CASES)
def test_three_page_states(fixture: str, url: str, state: PageState, title: str) -> None:
    verdict = classify(read(fixture), url)
    assert verdict.state is state
    assert verdict.title == title


def test_maintenance_page_is_not_a_missing_citation() -> None:
    """THE test. The maintenance page is 819 bytes -- smaller than the 3,549-byte
    soft-404 -- so any length-based rule calls it a fabricated citation. During an
    outage that reports every real case as hallucinated, with total confidence."""
    maintenance = read("maintenance_notice.html")
    soft_404 = read("soft404_2019_SGCA_999.html")
    assert len(maintenance) < len(soft_404)  # length cannot separate them

    verdict = classify(maintenance, SPANDECK_URL)
    assert verdict.state is PageState.UNAVAILABLE
    assert verdict.detail == "maintenance"

    document = parse(maintenance, SPANDECK_URL, http_status=200)
    assert document.exists is False
    assert document.is_soft_404 is False  # the flag L1 is allowed to fail on


def test_soft_404_is_the_only_fail_path() -> None:
    document = parse(read("soft404_2019_SGCA_999.html"), FAKE_URL, http_status=200)
    assert document.http_status == 200  # F3: the status code carries no signal
    assert document.exists is False
    assert document.is_soft_404 is True


def test_nested_page_not_found_title_does_not_fool_the_classifier() -> None:
    """The soft-404 embeds a second HTML document with its own <title>Page Not Found</
    title>. Reading the last title instead of the first turns an empty title into a
    non-empty one and blurs the two states this classifier exists to separate."""
    verdict = classify(read("soft404_2019_SGCA_999.html"), FAKE_URL)
    assert verdict.title == ""
    assert verdict.state is PageState.NOT_FOUND


@pytest.mark.parametrize(
    "html",
    [
        "<html><head><title>Sign in</title></head><body>Please log in</body></html>",
        "<html><head><title>Error 500</title></head><body>Oops</body></html>",
        "<html><head><title></title></head><body>totally unexpected markup</body></html>",
        "",
    ],
)
def test_unknown_pages_default_to_unavailable_never_not_found(html: str) -> None:
    """The default has to be the WARN branch. Only positive evidence of non-existence
    -- an empty title AND a Page Not Found body -- may conclude a citation is fake."""
    assert classify(html, SPANDECK_URL).state is PageState.UNAVAILABLE


def test_title_mismatch_is_a_judgment_not_a_missing_citation() -> None:
    """A real judgment that is not the one we asked for is RESOLVED_WRONG_DOC's problem.
    Calling it NOT_FOUND would convert a redirect into an accusation."""
    verdict = classify(read("2007_SGCA_37.html"), NG_URL)
    assert verdict.state is PageState.JUDGMENT
    assert verdict.citation_matches is False
    assert verdict.detail.startswith("citation_mismatch:")


# --- F5: structured extraction ---------------------------------------------


def test_spandeck_structure() -> None:
    document = parse(read("2007_SGCA_37.html"), SPANDECK_URL, http_status=200)
    assert document.exists is True
    assert document.neutral_citation == "[2007] SGCA 37"
    assert document.case_name is not None and document.case_name.startswith("Spandeck")
    assert document.court == "SGCA"
    assert document.year == 2007
    assert "Chan Sek Keong CJ" in (document.coram or "")
    assert document.domain == "www.elitigation.sg"
    assert document.text_sha256 and len(document.text) > 50_000


def test_pinpoint_lookup_resolves_a_paragraph_number() -> None:
    """'at [115]' has to reach paragraph 115 -- that narrowing is where most of L1's
    precision comes from."""
    document = parse(read("2007_SGCA_37.html"), SPANDECK_URL)
    paragraph = document.paragraph(115)
    assert paragraph is not None
    assert paragraph.kind is ChunkKind.BODY
    assert paragraph.text.startswith("To recapitulate")
    assert not paragraph.text.startswith("115")  # the leader is stripped from the text
    assert paragraph.heading_path == ("Conclusion",)


def test_paragraph_numbers_are_dense_and_ordered() -> None:
    document = parse(read("2007_SGCA_37.html"), SPANDECK_URL)
    numbers = [p.paragraph_number for p in document.paragraphs if p.paragraph_number]
    assert numbers == sorted(numbers)
    assert numbers[0] == 1
    assert len(numbers) > 100


def test_quotes_and_headings_are_classified() -> None:
    document = parse(read("2007_SGCA_37.html"), SPANDECK_URL)
    kinds = {kind: 0 for kind in (ChunkKind.BODY, ChunkKind.QUOTE, ChunkKind.HEADING)}
    for paragraph in document.paragraphs:
        kinds[paragraph.kind] += 1
    assert kinds[ChunkKind.QUOTE] == 49  # F5: Judg-Quote-1 x49
    assert kinds[ChunkKind.HEADING] == 47  # F5: Judg-Heading-1/2/3 x 8/17/22
    assert kinds[ChunkKind.BODY] > 100


def test_quoted_blocks_do_not_claim_a_judgment_paragraph_number() -> None:
    """Judg-Quote paragraphs carry the SOURCE document's numbering ('34.1 Reference to
    the Superintending Officer'). Reading that as a judgment paragraph would make
    'at [34]' resolve to a contract clause."""
    document = parse(read("2007_SGCA_37.html"), SPANDECK_URL)
    quotes = [p for p in document.paragraphs if p.kind is ChunkKind.QUOTE]
    assert quotes and all(p.paragraph_number is None for p in quotes)


def test_nobr_citations_become_cited_authorities() -> None:
    """F5 sees <nobr> around report citations. In the body those are the authorities
    the judgment CITES, not its own parallel citation -- indexing them as parallel
    citations would claim '[1992] 2 NZLR 282' resolves to Spandeck."""
    document = parse(read("2007_SGCA_37.html"), SPANDECK_URL)
    assert "[2007] 1 SLR 720" in document.cited_authorities
    assert document.neutral_citation not in document.cited_authorities


def test_modern_markup_generation_parses_too() -> None:
    """The 2021-era pages use <div class="Judg-1 mb-3 text-justify"> and HN-* metadata
    instead of <p class="Judg-1"> and an info-table. Matching the whole class attribute
    works for one generation and silently returns zero paragraphs for the other."""
    document = parse(read("2021_SGHC_100.html"), NG_URL, http_status=200)
    assert document.neutral_citation == "[2021] SGHC 100"
    assert document.case_name == "Ng Kum Weng v Public Prosecutor"
    assert document.court == "SGHC"
    assert document.coram == "Kannan Ramesh J"
    assert document.paragraph(1) is not None
    assert len([p for p in document.paragraphs if p.paragraph_number]) > 50


def test_parse_document_returns_classification_and_document() -> None:
    verdict, document = parse_document(read("2007_SGCA_37.html"), SPANDECK_URL, http_status=200)
    assert verdict.state is PageState.JUDGMENT
    assert verdict.citation_matches is True
    assert document.exists is True
