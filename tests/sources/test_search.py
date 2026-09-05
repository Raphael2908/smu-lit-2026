"""Search URL construction, and a deliberately markup-agnostic result parser.

VERIFICATION NOTE: the GET invocation is verified live (F6). The result-row MARKUP is
NOT -- eLitigation entered a maintenance window before it could be captured, and the
repo has no search-results fixture. These tests therefore assert the parser works
against several DIFFERENT plausible shapes rather than one guessed fixture, since the
only thing that cannot change without the URL scheme changing is the href itself.
"""

from __future__ import annotations

import pytest

from verifier.sources.elitigation.search import build_search_url, parse_search_results

BASE = "https://www.elitigation.sg"


def test_search_url_is_the_verified_get_invocation() -> None:
    url = build_search_url("Tan Cheng Bock v AG", base_url=BASE)
    assert url.startswith(f"{BASE}/gd/Home/Index?SearchPhrase=")
    for fragment in (
        "SearchPhrase=Tan+Cheng+Bock+v+AG",
        "CurrentPage=1",
        "SortBy=Score",
        "YearOfDecision=All",
        "SortAscending=False",
        "Verbose=False",
        "hdnFilter=SUPCT",
    ):
        assert fragment in url


def test_search_phrase_is_url_encoded() -> None:
    assert "%26" in build_search_url("Defence Science & Technology", base_url=BASE)


ROW = "Spandeck Engineering (S) Pte Ltd v Defence Science &amp; Technology Agency"

MARKUP_SHAPES = [
    pytest.param(
        f'<table><tr><td><a href="/gd/s/2007_SGCA_37">{ROW}</a></td></tr></table>',
        id="table-row",
    ),
    pytest.param(
        f"<div class='card'><a class='x' href='/gd/s/2007_SGCA_37'>{ROW}</a></div>",
        id="single-quoted-attribute",
    ),
    pytest.param(
        f'<ul><li><a href="/gd/s/2007_SGCA_37?SearchTerm=x">{ROW}</a></li></ul>',
        id="href-with-query-string",
    ),
    pytest.param(
        f'<div><h3>{ROW}</h3><a href="/gd/s/2007_SGCA_37"><img src="i.png"/></a></div>',
        id="name-outside-the-anchor",
    ),
    pytest.param(
        f'<a href="{BASE}/gd/s/2007_SGCA_37">{ROW}</a>',
        id="absolute-href",
    ),
]


@pytest.mark.parametrize("html", MARKUP_SHAPES)
def test_parser_survives_different_row_markup(html: str) -> None:
    hits = parse_search_results(f"<html><body>{html}</body></html>")
    assert len(hits) == 1
    assert hits[0].neutral_citation == "[2007] SGCA 37"
    assert hits[0].url.endswith("/gd/s/2007_SGCA_37")
    assert hits[0].rank == 1
    assert "Spandeck" in hits[0].case_name


def test_no_results_page_yields_zero_hits() -> None:
    """F6: zero hits for a well-formed case name is the fabrication signal, so this
    has to be exact -- and it must come from the page, never from a parse failure."""
    html = "<html><body><div class='no-results'>No results found.</div></body></html>"
    assert parse_search_results(html) == []


def test_duplicate_links_to_one_judgment_count_once() -> None:
    """Rows link the same judgment several times (title, PDF, thumbnail). Counting
    those separately inflates rank and turns one confident match into 'ambiguous'."""
    html = (
        f'<tr><td><a href="/gd/s/2007_SGCA_37">{ROW}</a>'
        '<a href="/gd/s/2007_SGCA_37">PDF</a></td></tr>'
        '<tr><td><a href="/gd/s/2021_SGHC_100">Ng Kum Weng v Public Prosecutor</a></td></tr>'
    )
    hits = parse_search_results(f"<html><body><table>{html}</table></body></html>")
    assert [h.neutral_citation for h in hits] == ["[2007] SGCA 37", "[2021] SGHC 100"]
    assert [h.rank for h in hits] == [1, 2]


def test_non_judgment_links_are_ignored() -> None:
    html = (
        '<a href="/gd/Home/Index">Home</a>'
        '<a href="https://example.com">Elsewhere</a>'
        '<a href="/gd/gd/2007_SGCA_37/pdf">PDF</a>'
    )
    assert parse_search_results(f"<html><body>{html}</body></html>") == []


def test_raw_html_fallback_when_hrefs_are_not_in_the_dom() -> None:
    """If the anchors move into JavaScript, degrade to 'hits without case names' --
    never to 'zero hits'. Inferring zero hits from our own parse failure would be a
    fabrication claim manufactured out of a bug."""
    html = "<html><body><script>var rows=['/gd/s/2007_SGCA_37'];</script></body></html>"
    hits = parse_search_results(html)
    assert len(hits) == 1
    assert hits[0].neutral_citation == "[2007] SGCA 37"
    assert hits[0].case_name == ""


def test_limit_is_respected() -> None:
    anchors = "".join(
        f'<a href="/gd/s/20{i:02d}_SGCA_{i}">Case Number {i} v Other Party {i}</a>'
        for i in range(1, 20)
    )
    assert len(parse_search_results(f"<html><body>{anchors}</body></html>", limit=3)) == 3


NOT_A_SEARCH_PAGE = [
    pytest.param(
        "<html><head><title>:: eLitigation - Maintenance Notice ::</title></head>"
        "<body>undergoing a system maintenance</body></html>",
        id="maintenance",
    ),
    pytest.param(
        "<html><head><title></title></head><body><h1>Page Not Found</h1></body></html>",
        id="soft-404",
    ),
    pytest.param("<html><body>totally unexpected markup</body></html>", id="unrecognised"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("html", NOT_A_SEARCH_PAGE)
def test_pages_that_are_not_the_search_app_are_rejected(html: str) -> None:
    """A zero-hit result may only be believed from a page that IS the search app.
    Otherwise an outage -- which serves the same notice on every path -- reads as
    'this case does not exist'."""
    from verifier.sources.elitigation.search import looks_like_search_page

    assert looks_like_search_page(html) is False


@pytest.mark.parametrize(
    "html",
    [
        '<html><body><span class="heading">SG Courts Judgments</span>'
        '<div class="no-results">No results found.</div></body></html>',
        '<html><body><form><input name="SearchPhrase"/></form></body></html>',
    ],
)
def test_real_search_pages_are_accepted(html: str) -> None:
    from verifier.sources.elitigation.search import looks_like_search_page

    assert looks_like_search_page(html) is True
