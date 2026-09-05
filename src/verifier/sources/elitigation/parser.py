"""eLitigation HTML -> SourceDocument, and the three-state page classifier.

This is the file where a naive implementation is dangerous rather than merely wrong.

A fetch of ``/gd/s/<slug>`` returns HTTP 200 whatever happened (F3), so the status code
carries no signal at all. Three different things arrive with the same 200:

    | State                | Bytes   | <title>                                |
    |----------------------|---------|----------------------------------------|
    | Real judgment        | 150,389 | ``[2007] SGCA 37``                     |
    | Fabricated citation  |   3,549 | ``''`` (empty)                         |
    | Site maintenance     |     819 | ``:: eLitigation - Maintenance Notice ::`` |

The obvious rule -- "small body means the citation does not exist" -- classifies the
maintenance page as a fabricated citation. During any maintenance window the system
would then report EVERY real Singapore case as hallucinated, with total confidence, and
it would look like a working demo right up until it didn't. That is the worst failure
this product can have, and F12 is a record of us nearly shipping it.

**The <title> is the discriminator. Length is only a corroborator.**

    title == the requested neutral citation      -> JUDGMENT     (the only RESOLVED)
    title empty AND "Page Not Found" in the body -> NOT_FOUND    (the only FAIL)
    title non-empty but not a citation           -> UNAVAILABLE  (WARN, never a FAIL)

Anything that does not positively match one of the first two states falls to
UNAVAILABLE. That default is deliberate and is the general rule the whole system runs
on: **"cannot verify" is never "fabricated."** Only positive evidence of non-existence
may fail a run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum

from selectolax.parser import HTMLParser, Node

from verifier.contracts.documents import Paragraph, SourceDocument
from verifier.contracts.enums import ChunkKind, FetchStrategy
from verifier.extraction import patterns
from verifier.sources.elitigation.citation_url import citation_from_url

#: Markers that positively identify eLitigation's soft-404 body. Both come from the
#: captured page in tests/corpus/soft404_2019_SGCA_999.html.
SOFT_404_MARKERS: tuple[str, ...] = (
    "Page Not Found",
    "page you are trying to access cannot be found",
)

_HEADING_CLASS = re.compile(r"^Judg-Heading-(?P<level>\d+)$")
_QUOTE_CLASS = re.compile(r"^Judg-Quote-(?P<level>\d+)$")
_BODY_CLASS = re.compile(r"^Judg-(?P<level>\d+)$")

#: "Kannan Ramesh J", "Andrew Phang Boon Leong JA", "Chan Sek Keong CJ".
_JUDGE = re.compile(
    r"\b[A-Z][A-Za-z’'\-.]*(?:\s+[A-Z][A-Za-z’'\-.]*){0,4}\s+(?:JAD|JCA|JA|JC|CJ|SJ|J)\b"
)


class PageState(StrEnum):
    """What actually came back, independent of the HTTP status code."""

    JUDGMENT = "judgment"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Classification:
    state: PageState
    #: The page's own <title>, stripped. '' for the soft-404.
    title: str
    #: The citation the URL asked for, e.g. "[2007] SGCA 37". None for non-judgment URLs.
    requested_citation: str | None
    #: The citation the page claims to be, parsed from its <title>.
    page_citation: str | None
    #: Machine-readable reason, carried into Resolution.detail for the UI.
    detail: str

    @property
    def citation_matches(self) -> bool:
        """True when the page we got back is the page we asked for (F4)."""
        if self.page_citation is None or self.requested_citation is None:
            return False
        return _normalise_citation(self.page_citation) == _normalise_citation(
            self.requested_citation
        )


def _normalise_citation(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def _document_title(tree: HTMLParser) -> str:
    """The FIRST <title> in the document.

    This matters: eLitigation's soft-404 embeds a whole second HTML document (with its
    own ``<title>Page Not Found</title>``) inside the outer page's body, so the parsed
    tree has two title nodes. The outer, empty one is the page's real title and the
    thing the classifier must read; taking the last would turn an empty title into
    "Page Not Found" and blur the two states the classifier exists to separate.
    """
    node = tree.css_first("title")
    return node.text(strip=True) if node else ""


def classify(html: str, url: str) -> Classification:
    """Decide which of the three page states we are looking at."""
    return classify_tree(HTMLParser(html), url)


def classify_tree(tree: HTMLParser, url: str) -> Classification:
    """``classify`` against an already-parsed tree, so callers parse the HTML once."""
    title = _document_title(tree)
    requested = citation_from_url(url)
    body_text = tree.body.text(separator=" ", strip=True) if tree.body else ""

    page_citation: str | None = None
    exact = patterns.NEUTRAL_CITATION_EXACT.match(title)
    if exact:
        page_citation = f"[{exact.group('year')}] {exact.group('court')} {exact.group('number')}"

    if page_citation is not None:
        detail = "ok"
        if requested and _normalise_citation(page_citation) != _normalise_citation(requested):
            # A real judgment, but not the one we asked for. Still a JUDGMENT page --
            # L1 compares the citations and raises RESOLVED_WRONG_DOC. Calling it
            # NOT_FOUND here would convert a redirect into an accusation.
            detail = f"citation_mismatch:{page_citation}"
        return Classification(PageState.JUDGMENT, title, requested, page_citation, detail)

    if not title and any(marker in body_text for marker in SOFT_404_MARKERS):
        # The ONLY branch that is allowed to conclude a citation does not exist.
        return Classification(PageState.NOT_FOUND, title, requested, None, "soft_404")

    # Everything else: maintenance notice, a login wall, a redirect, an error page, a
    # markup change we have never seen. All of them mean "we could not check", which is
    # a WARN. Never a FAIL.
    reason = "maintenance" if "maintenance" in title.lower() else "source_unavailable"
    return Classification(PageState.UNAVAILABLE, title, requested, None, reason)


# ---------------------------------------------------------------------------
# Structured extraction (F5)
# ---------------------------------------------------------------------------


def _text(node: Node) -> str:
    return re.sub(r"\s+", " ", node.text(separator=" ", strip=False)).strip()


def _judg_class(node: Node) -> str | None:
    """The ``Judg-*`` class token of a node, ignoring the Bootstrap utility classes.

    eLitigation's 2021-era markup writes ``class="Judg-1 mb-3 text-justify  "`` while the
    2007-era markup writes ``class="Judg-1"``. Matching on the whole attribute works for
    one generation and silently returns zero paragraphs for the other.
    """
    raw = node.attributes.get("class") or ""
    return next((token for token in raw.split() if token.startswith("Judg-")), None)


def _emittable(judg: str | None) -> bool:
    """True for the classes we turn into Paragraphs.

    ``Judg-Author``, ``Judg-Lawyers``, ``Judg-Date-Reserved`` and the wrapper table
    ``Judg-quote-list0`` are all ``Judg-*`` but are metadata or layout, not text.
    """
    if judg is None:
        return False
    return bool(_HEADING_CLASS.match(judg) or _QUOTE_CLASS.match(judg) or _BODY_CLASS.match(judg))


def _has_emittable_ancestor(node: Node) -> bool:
    """Guard against emitting a paragraph twice when one nests inside another.

    Deliberately stateless. selectolax builds a fresh ``Node`` wrapper on every
    ``.parent`` access, so an identity set keyed on ``id(node)`` is not just useless but
    actively wrong: CPython reuses the ids of collected temporaries, so unrelated
    paragraphs start colliding with entries in the set and get silently dropped. That is
    exactly how a 131-paragraph judgment turns into 49 paragraphs -- with no error, and
    with every downstream pinpoint lookup quietly failing.
    """
    parent = node.parent
    while parent is not None:
        if _emittable(_judg_class(parent)):
            return True
        parent = parent.parent
    return False


def _extract_paragraphs(tree: HTMLParser) -> list[Paragraph]:
    """Numbered paragraphs, quoted blocks and headings, in document order.

    Paragraph numbers are read only off ``Judg-1`` nodes. ``Judg-2`` is the sub-list
    level and its leaders are "(a)", "(b)"; quoted blocks carry the *source document's*
    numbering ("34.1 Reference to the Superintending Officer"). Reading either as a
    judgment paragraph number would make ``at [34]`` resolve to a contract clause.
    """
    if tree.body is None:
        return []

    paragraphs: list[Paragraph] = []
    heading_stack: dict[int, str] = {}
    ordinal = 0

    for node in tree.body.traverse(include_text=False):
        if node.tag not in ("p", "div"):
            continue
        judg = _judg_class(node)
        if not _emittable(judg) or _has_emittable_ancestor(node):
            continue

        heading = _HEADING_CLASS.match(judg)
        if heading:
            level = int(heading.group("level"))
            text = _text(node)
            if not text:
                continue
            for deeper in [k for k in heading_stack if k >= level]:
                del heading_stack[deeper]
            heading_stack[level] = text
            paragraphs.append(
                Paragraph(
                    ordinal=ordinal,
                    kind=ChunkKind.HEADING,
                    heading_path=tuple(heading_stack[k] for k in sorted(heading_stack)),
                    text=text,
                )
            )
            ordinal += 1
            continue

        quote = _QUOTE_CLASS.match(judg)
        body = _BODY_CLASS.match(judg)
        if quote is None and body is None:
            continue  # Judg-Author, Judg-Lawyers, Judg-Date-Reserved: metadata, not text

        text = _text(node)
        if not text:
            continue

        number: int | None = None
        if body is not None and int(body.group("level")) == 1:
            leader = patterns.JUDGMENT_PARA_NUMBER.match(text)
            if leader:
                number = int(leader.group("number"))
                text = text[leader.end() :].strip()

        paragraphs.append(
            Paragraph(
                ordinal=ordinal,
                paragraph_number=number,
                kind=ChunkKind.QUOTE if quote else ChunkKind.BODY,
                heading_path=tuple(heading_stack[k] for k in sorted(heading_stack)),
                text=text,
            )
        )
        ordinal += 1

    return paragraphs


def _info_rows(tree: HTMLParser) -> dict[str, str]:
    """The 2007-era ``info-row`` / ``txt-label`` metadata table."""
    rows: dict[str, str] = {}
    for row in tree.css("tr.info-row"):
        label = row.css_first("td.txt-label")
        value = row.css_first("td.txt-body")
        if label is None or value is None:
            continue
        key = _text(label).rstrip(":").strip()
        if key:
            rows[key] = _text(value)
    return rows


def _case_name(tree: HTMLParser) -> str | None:
    node = tree.css_first("span.caseTitle")  # 2007-era markup
    if node is not None and _text(node):
        return _text(node)
    for candidate in tree.css("div.HN-CaseName"):  # 2021-era markup
        text = _text(candidate)
        # The block repeats: parties first, then the neutral citation on its own.
        if text and not patterns.NEUTRAL_CITATION_EXACT.match(text):
            return re.sub(r"\s+", " ", text).strip()
    return None


def _coram(tree: HTMLParser, rows: dict[str, str]) -> str | None:
    if rows.get("Coram"):
        return re.sub(r"\s*;\s*", "; ", rows["Coram"]).strip("; ")
    author = tree.css_first("div.Judg-Author")  # 2021-era: "Kannan Ramesh J:"
    if author is not None and _text(author):
        return _text(author).rstrip(":").strip()
    coram = tree.css_first("div.HN-Coram")
    if coram is not None:
        for line in coram.text(separator="\n", strip=True).splitlines():
            cleaned = re.sub(r"\s+", " ", line).strip()
            if _JUDGE.fullmatch(cleaned):
                return cleaned
    return None


def _nobr_citations(tree: HTMLParser) -> list[str]:
    """Report citations wrapped in ``<nobr>`` (F5).

    Note what these actually are: eLitigation wraps EVERY report citation in the body,
    which in practice means the authorities the judgment cites, not the judgment's own
    parallel citation. They are therefore returned as ``cited_authorities``. Putting
    them in ``parallel_citations`` would build a reverse index claiming, for example,
    that "[1992] 2 NZLR 282" resolves to Spandeck -- a wrong-document resolution
    manufactured out of a naming assumption.
    """
    out: list[str] = []
    for node in tree.css("nobr"):
        text = _text(node)
        if text and (
            patterns.REPORT_CITATION.search(text) or patterns.NEUTRAL_CITATION.search(text)
        ):
            out.append(text)
    return out


def _parallel_citations(tree: HTMLParser, own: str | None) -> list[str]:
    """Report citations printed in the page's own header block.

    Scoped to the header on purpose -- see ``_nobr_citations``. Neither corpus page
    carries one, so this is usually empty; it is here so that when eLitigation does
    print one, it lands in the field that means what its name says.
    """
    seen: dict[str, None] = {}
    for selector in ("h2.title", "div.HN-NeutralCit", "div.HN-CaseName", "#info-table"):
        for node in tree.css(selector):
            text = _text(node)
            for match in patterns.REPORT_CITATION.finditer(text):
                if own is None or _normalise_citation(match.group(0)) != _normalise_citation(own):
                    seen.setdefault(match.group(0), None)
    return list(seen)


def parse(
    html: str,
    url: str,
    *,
    http_status: int | None = None,
    fetch_strategy: FetchStrategy = FetchStrategy.HTTP,
) -> SourceDocument:
    """Parse an eLitigation response into a ``SourceDocument``.

    Always returns a document. ``exists`` is False for both the soft-404 and the
    unavailable states; ``is_soft_404`` is what separates them, and it is the only flag
    a caller may treat as evidence of fabrication.
    """
    return parse_document(html, url, http_status=http_status, fetch_strategy=fetch_strategy)[1]


def parse_document(
    html: str,
    url: str,
    *,
    http_status: int | None = None,
    fetch_strategy: FetchStrategy = FetchStrategy.HTTP,
) -> tuple[Classification, SourceDocument]:
    """Parse once and return both the page classification and the document.

    The classification carries WHY a page is not a judgment (soft-404 vs maintenance vs
    unknown), which is the distinction between a FAIL and a WARN. Returning it alongside
    the document keeps a caller from having to re-derive it from ``exists`` and
    ``is_soft_404``, and from having to parse 150kB of HTML a second time to do so.
    """
    tree = HTMLParser(html)
    verdict = classify_tree(tree, url)
    domain = _domain(url)

    if verdict.state is not PageState.JUDGMENT:
        return verdict, SourceDocument(
            source_url=url,
            domain=domain,
            fetch_strategy=fetch_strategy,
            exists=False,
            is_soft_404=verdict.state is PageState.NOT_FOUND,
            http_status=http_status,
            text="",
            text_sha256=hashlib.sha256(b"").hexdigest(),
        )

    rows = _info_rows(tree)
    paragraphs = _extract_paragraphs(tree)
    text = "\n\n".join(p.text for p in paragraphs)

    citation = verdict.page_citation
    court: str | None = None
    year: int | None = None
    if citation:
        match = patterns.NEUTRAL_CITATION.search(citation)
        if match:
            # The court CODE, not the prose name, so it compares directly against
            # ExtractedCitation.court and the citation_key both sides build from it.
            court = match.group("court")
            year = int(match.group("year"))
    if court is None:
        court = rows.get("Tribunal/Court") or None

    authorities: dict[str, None] = {}
    for value in _nobr_citations(tree):
        authorities.setdefault(value, None)
    for match in patterns.NEUTRAL_CITATION.finditer(text):
        if citation is None or _normalise_citation(match.group(0)) != _normalise_citation(citation):
            authorities.setdefault(match.group(0), None)

    return verdict, SourceDocument(
        source_url=url,
        domain=domain,
        fetch_strategy=fetch_strategy,
        exists=True,
        is_soft_404=False,
        http_status=http_status,
        neutral_citation=citation,
        case_name=_case_name(tree),
        court=court,
        year=year,
        coram=_coram(tree, rows),
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        paragraphs=tuple(paragraphs),
        parallel_citations=tuple(_parallel_citations(tree, citation)),
        cited_authorities=tuple(authorities),
    )


def _domain(url: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(url if "//" in url else "//" + url).hostname or ""
    return host.lower()
