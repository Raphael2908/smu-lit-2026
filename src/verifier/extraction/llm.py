"""L0 with a model in the loop: Haiku finds the citations, the parser types them.

The division of labour is the whole design. FINDING a citation is a recognition problem
over prose, and a regex can only recognise the forms someone enumerated -- 28 report
series, a fixed court list, four single-token party names (docs/03-findings.md F13).
TYPING one is a parsing problem with a right answer, and ``extract_citations`` already
does it against the SAL SLR Style Guide. So the model says where to look and the
deterministic parser says what it is.

Three rules hold this together, and each closes a way the feature could accuse correct
legal work of fabrication.

**Verbatim, or it does not exist.** Every candidate is located as a substring of the
output, and its span comes from that search. Spans are never taken from the model:
``ExtractedCitation.span`` drives quote attribution and proposition scope, so a guessed
offset silently corrupts both. It also means a downstream CITATION_NOT_FOUND is a
statement about the OUTPUT's citation rather than about the model's transcription.

**Typed by the parser, never by the model.** ``build_url``
(sources/elitigation/citation_url.py) checks only that a citation is NEUTRAL, never that
its court is Singaporean. A model-typed "[2019] UKSC 32" would become
elitigation.sg/gd/s/2019_UKSC_32, return a soft-404 (F3), and report a real UK Supreme
Court case as fabricated. Running ``extract_citations`` over the located text makes that
unreachable, because NEUTRAL_CITATION only matches the enumerated SG courts.

**Untypable is not the same as absent.** A candidate the parser cannot type -- an
unenumerated series, a practice direction, a textbook -- is real authority we cannot
check. Clustering it would send the phrase to a case-name search, and zero hits is
precisely what this system reads as fabrication (F6). So it goes to
``ExtractionResult.untyped``: it counts for L1a and is never fetched.

And when the extractor does not run at all, the result carries ``extractor_degraded``
and NO citations. There is deliberately no regex fallback: falling back would report a
run as deterministically checked when it was not. L1a reads the flag and declines to
fail, which is the same rule as everywhere else here -- "cannot verify" is never
"fabricated".
"""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass
from typing import Any

from verifier.contracts.citations import ExtractedCitation, Span
from verifier.contracts.layers import ExtractionResult
from verifier.extraction import assemble, extract
from verifier.extraction.citations import cluster_citations, extract_citations
from verifier.extraction.propositions import extract_statutes
from verifier.logging import get_logger
from verifier.providers.base import CitationCandidate, CitationExtractor

__all__ = ["Placement", "extract_with_llm", "place_candidates"]

log = get_logger(__name__)


class _Default:
    """Sentinel. ``extractor=None`` means 'deliberately no model', which is not the
    same as 'use the configured one', and only a third value can tell them apart."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "DEFAULT"


DEFAULT: Any = _Default()


#: Characters a model writes where the answer used something else. Folding these is what
#: lets a citation copied out of rendered markdown be found in the source text.
_TRANSLATE = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "′": "'",
    "″": '"',
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
}


def _fold(text: str) -> tuple[str, list[int]]:
    """Casefold, translate lookalikes and collapse whitespace, keeping an offset map.

    ``origin[i]`` is the index in ``text`` that produced ``folded[i]``. Done per source
    character because NFKD and casefolding both change length ("ﬁ" -> "fi", "ß" -> "ss"),
    and an offset map that drifts is worse than none: it would place a citation's span
    over the wrong words and mis-attribute every quote near it.

    Whitespace collapse is required, not cosmetic. Markdown wraps long case names across
    lines, so the citation in the source text contains a newline where the model's copy
    has a space -- the exact case citations.py:98-104 calls out.
    """
    chars: list[str] = []
    origin: list[int] = []
    pending_space = False
    for index, raw in enumerate(text):
        char = _TRANSLATE.get(raw, raw)
        if char.isspace():
            pending_space = bool(chars)
            continue
        if pending_space:
            chars.append(" ")
            origin.append(index)
            pending_space = False
        for expanded in unicodedata.normalize("NFKD", char).casefold():
            chars.append(expanded)
            origin.append(index)
    return "".join(chars), origin


def _locate(
    folded: str,
    origin: list[int],
    needle: str,
    taken: list[tuple[int, int]],
) -> Span | None:
    """First occurrence of ``needle`` in the output not already claimed.

    Returns None when the text is not there at all. That is the anti-hallucination
    guard and it is load-bearing: a citation the model invented, or "helpfully"
    corrected, cannot be found and therefore never becomes authority.

    Skipping claimed spans is what makes a citation repeated three times in an answer
    resolve to three distinct spans rather than three copies of the first.
    """
    folded_needle, _ = _fold(needle)
    if not folded_needle:
        return None
    start = 0
    while True:
        hit = folded.find(folded_needle, start)
        if hit < 0:
            return None
        src_start = origin[hit]
        src_end = origin[hit + len(folded_needle) - 1] + 1
        if not any(ts < src_end and src_start < te for ts, te in taken):
            return Span(start=src_start, end=src_end)
        start = hit + 1


@dataclass(frozen=True)
class Placement:
    """The outcome of placing one model response against the answer.

    ``missing`` and ``duplicate`` are counted apart deliberately. A duplicate is the
    model doing something sensible -- it returns "Spandeck ... [2007] SGCA 37" and then
    "[2007] SGCA 37" again, and the second has no unclaimed occurrence left. A missing
    candidate is text the answer does not contain, which is the one signal that would
    show a model drifting away from copying verbatim. Summed together they would hide
    exactly the thing worth watching.
    """

    citations: list[ExtractedCitation]
    untyped: list[tuple[Span, str]]
    missing: int = 0
    duplicate: int = 0


def place_candidates(text: str, candidates: tuple[CitationCandidate, ...]) -> Placement:
    """Locate, type and order the model's candidates.

    Typing runs ``extract_citations`` over the LOCATED text rather than over the model's
    string, so ``raw_text`` is always the output's own characters. One candidate may
    yield several citations -- "Spandeck ... v ... [2007] SGCA 37" is a case name and a
    neutral citation -- which is what the clusterer expects to see.
    """
    folded, origin = _fold(text)
    taken: list[tuple[int, int]] = []
    placed: list[ExtractedCitation] = []
    untyped: list[tuple[Span, str]] = []
    missing = 0
    duplicate = 0

    for candidate in candidates:
        span = _locate(folded, origin, candidate.raw_text, taken)
        if span is None:
            if _locate(folded, origin, candidate.raw_text, []) is None:
                missing += 1
            else:
                duplicate += 1
            continue
        taken.append((span.start, span.end))
        snippet = text[span.start : span.end]
        typed = extract_citations(snippet)
        if not typed:
            untyped.append((span, snippet))
            continue
        for citation in typed:
            placed.append(
                citation.model_copy(
                    update={
                        "span": Span(
                            start=span.start + citation.span.start,
                            end=span.start + citation.span.end,
                        )
                    }
                )
            )

        # A link the OUTPUT carries alongside the citation. Located like everything
        # else: a URL the model supplied but the answer never wrote is not in the
        # answer, and fetching it would check a source the lawyer never cited.
        if candidate.url:
            url_span = _locate(folded, origin, candidate.url, taken)
            if url_span is not None:
                taken.append((url_span.start, url_span.end))
                for citation in extract_citations(text[url_span.start : url_span.end]):
                    placed.append(
                        citation.model_copy(
                            update={
                                "span": Span(
                                    start=url_span.start + citation.span.start,
                                    end=url_span.start + citation.span.end,
                                )
                            }
                        )
                    )

    placed.sort(key=lambda c: (c.span.start, c.span.end))
    return Placement(
        citations=[c.model_copy(update={"ordinal": i}) for i, c in enumerate(placed)],
        untyped=untyped,
        missing=missing,
        duplicate=duplicate,
    )


def _build_extractor() -> tuple[CitationExtractor | None, str | None]:
    """The configured extractor, or the reason there isn't one.

    A provider that cannot be built -- no key, an import that is not there -- must not
    take the run down, and must NOT quietly become the deterministic pass either. It is
    the same state as a timeout: we did not look. Returning the reason is what lets
    ``extractor_degraded`` say so instead of the run reporting citations it never
    checked for.
    """
    try:
        from verifier.providers.factory import get_citation_extractor

        return get_citation_extractor(), None
    except Exception as exc:  # noqa: BLE001 - a missing key must not fail the run
        log.warning("citation_extractor_unavailable", error=str(exc))
        return None, f"citation extractor unavailable: {exc}"


async def extract_with_llm(
    text: str, *, extractor: CitationExtractor | None | Any = DEFAULT
) -> ExtractionResult:
    """Full L0 pass with the model finding the citations.

    ``extractor=None`` means "deliberately run the deterministic pass" -- an explicit
    choice by the caller, not a fallback from failure.
    """
    from verifier.settings import get_settings

    if isinstance(extractor, _Default):
        resolved, unavailable = _build_extractor()
        if resolved is None:
            return assemble(text, [], extractor_degraded=unavailable)
    elif extractor is None:
        # The caller asked for the deterministic pass by name. Not a fallback.
        return extract(text)
    else:
        resolved = extractor

    settings = get_settings()
    degraded: str | None = None
    result = None
    try:
        result = await asyncio.wait_for(
            resolved.extract_citations(text), timeout=settings.EXTRACTOR_TIMEOUT_S
        )
    except TimeoutError:
        degraded = f"citation extractor timed out after {settings.EXTRACTOR_TIMEOUT_S}s"
    except Exception as exc:  # noqa: BLE001 - nothing here may take the run down
        degraded = f"citation extractor failed: {exc}"
    else:
        degraded = result.degraded

    if degraded or result is None:
        log.warning("citation_extractor_degraded", reason=degraded)
        return assemble(
            text, [], extractor_degraded=degraded or "citation extractor returned nothing"
        )

    placement = place_candidates(text, result.citations)
    if placement.missing:
        # Not an error: the guard doing its job. Worth a line on its own, because a
        # model drifting away from copying verbatim shows up here first and nowhere else.
        log.info(
            "citation_candidates_missing",
            missing=placement.missing,
            duplicate=placement.duplicate,
            returned=len(result.citations),
            model=result.model,
        )

    # Statutes are extracted deterministically from the same text, so a statutory
    # reference the model also returned is already counted. Leaving it in `untyped` too
    # would inflate authority_count and show the reader the same citation twice.
    statute_spans = [(st.span.start, st.span.end) for st in extract_statutes(text)]
    untyped = tuple(
        raw
        for span, raw in placement.untyped
        if not any(ss < span.end and span.start < se for ss, se in statute_spans)
    )
    return assemble(text, cluster_citations(placement.citations, text=text), untyped=untyped)
