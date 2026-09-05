"""Case-name search against eLitigation's judgment index.

F6: the search is binary and that is what makes it useful. "Tan Cheng Bock v AG"
returned ``2017_SGCA_50`` at rank 1; two fabricated case names returned zero hits each.
Zero hits for a well-formed case name is our strongest fabrication signal.

Which is also why ``extraction/patterns.py`` is so conservative about what counts as a
case name. The search is only a fabrication signal if the phrase we searched was really
a case name; feed it a sentence fragment and the zero result means nothing at all, while
looking exactly like proof.

F7 bounds it: the index is full-text over judgment BODIES, so searching a report
citation returns the cases that CITE it, never the case itself. Report citations must
never be sent here and must never be failed.

VERIFICATION NOTE: the GET invocation below is verified live. The result-row MARKUP is
NOT -- eLitigation went into a maintenance window before it could be re-captured, and
this repo has no search-results fixture. The parser is therefore written against the one
thing that cannot change without the URL scheme changing: hrefs of the form
``/gd/s/{YEAR}_{COURT}_{NUM}``. It reads those anchors wherever they appear in the
document, takes the case name from whichever enclosing row/cell/heading actually has
text, and falls back to a raw-HTML regex if the anchors are not in the DOM at all.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urlsplit

from selectolax.parser import HTMLParser, Node

from verifier.providers.base import SearchHit
from verifier.settings import settings
from verifier.sources.elitigation.citation_url import JUDGMENT_PATH, citation_from_url

#: The verified working invocation. GET, not POST. ``hdnFilter=SUPCT`` scopes the search
#: to the Supreme Court collection, which is the corpus the judgment URLs live in.
SEARCH_PATH = "/gd/Home/Index"
SEARCH_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("CurrentPage", "1"),
    ("SortBy", "Score"),
    ("YearOfDecision", "All"),
    ("SortAscending", "False"),
    ("Verbose", "False"),
    ("hdnFilter", "SUPCT"),
)

#: Fallback when the anchors are not reachable through the DOM (framed results, hrefs
#: assembled in JavaScript, a markup change we have not seen).
_HREF_IN_HTML = re.compile(r"""/gd/s/(?P<slug>\d{4}_[A-Za-z]+_\d{1,4})""")

#: Positive markers that the response really is the judgments search application. A
#: zero-hit result may only be trusted when one of these is present -- see
#: ``looks_like_search_page``.
SEARCH_PAGE_MARKERS: tuple[str, ...] = (
    "SG Courts Judgments",
    "SearchPhrase",
    "<form",
)

#: Markers that the site is not serving the application at all.
OUTAGE_MARKERS: tuple[str, ...] = (
    "Maintenance Notice",
    "maintenance",
    "temporarily unavailable",
    "Page Not Found",
)

#: Chrome that appears inside result rows and is never part of a case name.
_NOISE = re.compile(
    r"\b(?:Download PDF|PDF|View|Judgment|Decision Date|Case Number|Coram|Read more)\b",
    re.IGNORECASE,
)


def build_search_url(phrase: str, *, base_url: str | None = None, page: int = 1) -> str:
    """The verified GET search URL for ``phrase``."""
    base = (base_url or settings.ELITIGATION_BASE_URL).rstrip("/")
    params = [("SearchPhrase", phrase)]
    params += [(k, str(page) if k == "CurrentPage" else v) for k, v in SEARCH_DEFAULTS]
    query = "&".join(f"{k}={quote_plus(v)}" for k, v in params)
    return f"{base}{SEARCH_PATH}?{query}"


def looks_like_search_page(html: str) -> bool:
    """True only when the response is recognisably the judgments search application.

    This guard is the search-side twin of the parser's three-state classifier, and it
    exists for the same reason. During the outage that produced F12 the maintenance
    notice is served for EVERY path, including this one. It contains no
    ``/gd/s/`` hrefs, so a parser that trusts any 200 reads it as "zero hits" -- and zero
    hits is our strongest fabrication signal. The outage would then accuse every real
    case the search path touches.

    So: an outage marker disqualifies the page outright, and a zero-hit result is only
    believed when the page carries the search app's own chrome. Anything unrecognised
    raises SearchUnavailable, which is a WARN. "Cannot verify" is never "fabricated".
    """
    if any(marker.casefold() in html.casefold() for marker in OUTAGE_MARKERS):
        return False
    return any(marker.casefold() in html.casefold() for marker in SEARCH_PAGE_MARKERS)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _NOISE.sub(" ", text)).strip(" |-–—· \t\n")


def _case_name_for(anchor: Node) -> str:
    """Best available case name for a result anchor.

    Tries the anchor's own text, then walks outward through enclosing elements until
    something with real text turns up. Written this way precisely because the row markup
    is unverified: any of ``<a>``, ``<td>``, ``<div class="card">`` or ``<li>`` could be
    where the name lives, and this finds it in all of them without knowing which.
    """
    own = _clean(anchor.text(separator=" ", strip=True))
    if len(own) > 3:
        return own
    node = anchor.parent
    depth = 0
    while node is not None and depth < 5:
        text = _clean(node.text(separator=" ", strip=True))
        if len(text) > 3:
            return text[:300]
        node = node.parent
        depth += 1
    return ""


def parse_search_results(html: str, *, limit: int = 10) -> list[SearchHit]:
    """Extract judgment hits from a search-results page, in rank order.

    Deduplicates by neutral citation: the same judgment is often linked more than once
    per row (title link, "PDF" link, a thumbnail), and counting those as separate hits
    would inflate rank and turn a single confident match into a false "ambiguous".
    """
    tree = HTMLParser(html)
    seen: dict[str, SearchHit] = {}

    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        if not JUDGMENT_PATH.search(href):
            continue
        citation = citation_from_url(href)
        if citation is None or citation in seen:
            continue
        seen[citation] = SearchHit(
            neutral_citation=citation,
            url=_absolute(href),
            case_name=_case_name_for(anchor),
            rank=len(seen) + 1,
        )
        if len(seen) >= limit:
            break

    if not seen:
        # DOM gave us nothing. Fall back to the raw text so a markup change downgrades
        # us to "no case names" instead of to "zero hits" -- zero hits is a fabrication
        # signal, and inferring it from our own parse failure would be an accusation
        # manufactured out of a bug.
        for match in _HREF_IN_HTML.finditer(html):
            citation = citation_from_url("/gd/s/" + match.group("slug"))
            if citation is None or citation in seen:
                continue
            seen[citation] = SearchHit(
                neutral_citation=citation,
                url=_absolute("/gd/s/" + match.group("slug")),
                case_name="",
                rank=len(seen) + 1,
            )
            if len(seen) >= limit:
                break

    return list(seen.values())


def _absolute(href: str) -> str:
    """Canonical judgment URL for a result href.

    Query strings and fragments are dropped: eLitigation decorates result links with the
    search term, and two spellings of the same judgment URL would defeat the adapter's
    per-document cache and spend the politeness budget fetching 150kB twice.
    """
    parts = urlsplit(href)
    path = parts.path if parts.path.startswith("/") else "/" + parts.path
    if parts.scheme:
        return f"{parts.scheme}://{parts.netloc}{path}"
    return f"{settings.ELITIGATION_BASE_URL.rstrip('/')}{path}"
