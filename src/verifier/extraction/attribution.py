"""Tying quotes to citations.

Four methods, in descending confidence, exactly as ``AttributionMethod`` orders them:

1. **PINPOINT** -- "at [115]" / "at para 115" within 100 characters of the quote. This
   is the valuable one. It does not merely say *which* case the quote came from, it says
   which paragraph, which collapses the search space from a ~84,000-character judgment
   to a few hundred characters. ``partial_ratio`` over a whole judgment will find a
   plausible-looking best match for almost anything; over one paragraph it will not.
2. **EXPLICIT** -- the citation and the quote sit in the same sentence.
3. **PROXIMITY** -- same paragraph, within 400 characters.
4. **NONE** -- unattributed. Per the contract this is INFO, never a failure: a quote we
   cannot tie to a source is a quote we cannot check, and "cannot check" is not
   "fabricated".
"""

from __future__ import annotations

from verifier.contracts.citations import CitationCluster, ExtractedQuote, Span
from verifier.contracts.enums import AttributionMethod
from verifier.extraction import patterns

#: Same-paragraph proximity budget, per the contract's PROXIMITY definition.
PROXIMITY_MAX_CHARS = 400


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in patterns.PARAGRAPH_BREAK.finditer(text):
        spans.append((cursor, match.start()))
        cursor = match.end()
    spans.append((cursor, len(text)))
    return [(s, e) for s, e in spans if e > s]


def _containing(spans: list[tuple[int, int]], position: int) -> tuple[int, int] | None:
    for start, end in spans:
        if start <= position <= end:
            return (start, end)
    return None


def _gap(a: Span, b: Span) -> int:
    """Character distance between two spans; 0 when they touch or overlap."""
    if a.end <= b.start:
        return b.start - a.end
    if b.end <= a.start:
        return a.start - b.end
    return 0


def _between(a: Span, b: Span) -> tuple[int, int]:
    return (a.end, b.start) if a.end <= b.start else (b.end, a.start)


def _find_pinpoint(
    text: str, quote: ExtractedQuote, bounds: tuple[int, int] | None = None
) -> tuple[int, int] | None:
    """Nearest ``at [N]`` outside the quote body, as (paragraph_number, position).

    We search before and after the quote separately and keep whichever is closer, since
    both orders occur naturally ("In Spandeck at [115] the court said: ..." and
    "... (Spandeck at [115])"). Matches inside the quote body are ignored: a pinpoint
    written *within* quoted text is the source citing something else.
    """
    window = patterns.PINPOINT_WINDOW_CHARS
    # Clamp the window to the quote's own paragraph. Without this a pinpoint belonging
    # to the NEXT paragraph's citation gets attached to this quote, and a confidently
    # wrong paragraph number is worse than none: it points verification at text the
    # quote was never taken from.
    low, high = bounds if bounds else (0, len(text))
    before_start = max(low, quote.span.start - window)
    before = text[before_start : quote.span.start]
    after = text[quote.span.end : min(high, quote.span.end + window)]

    best: tuple[int, int, int] | None = None  # distance, number, position
    matches = [(m, before_start) for m in patterns.PINPOINT.finditer(before)]
    matches += [(m, quote.span.end) for m in patterns.PINPOINT.finditer(after)]
    for match, offset in matches:
        raw = match.group("bracketed") or match.group("plain")
        if raw is None:
            continue
        keyword = match.group("kw1") or match.group("kw2")
        low, high = patterns.PINPOINT_YEAR_RANGE
        if keyword is None and low <= int(raw) <= high:
            continue
        position = offset + match.start()
        distance = (
            quote.span.start - (offset + match.end())
            if offset < quote.span.start
            else position - quote.span.end
        )
        candidate = (abs(distance), int(raw), position)
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        return None
    return best[1], best[2]


def attribute_quotes(
    text: str,
    quotes: list[ExtractedQuote],
    clusters: list[CitationCluster],
) -> list[ExtractedQuote]:
    """Return ``quotes`` with attribution fields populated.

    ``ExtractedQuote`` is frozen, so this returns copies rather than mutating.
    """
    if not quotes:
        return []

    paragraphs = _paragraph_spans(text)
    out: list[ExtractedQuote] = []

    for quote in quotes:
        quote_paragraph = _containing(paragraphs, quote.span.start)
        pinpoint = _find_pinpoint(text, quote, quote_paragraph)
        pinpoint_paragraph = pinpoint[0] if pinpoint else None

        def in_same_paragraph(cluster: CitationCluster, para=quote_paragraph) -> bool:
            if para is None:
                return False
            return _containing(paragraphs, cluster.span.start) == para

        same_paragraph = [c for c in clusters if in_same_paragraph(c)]

        chosen: CitationCluster | None = None
        method = AttributionMethod.NONE

        if pinpoint is not None and same_paragraph:
            # Attribute to the citation nearest the pinpoint itself: the pinpoint is
            # written next to the case it refers to ("Spandeck at [115]"), which may not
            # be the citation nearest the quote when several are in play.
            position = pinpoint[1]
            chosen = min(
                same_paragraph,
                key=lambda c, p=position: min(abs(c.span.start - p), abs(c.span.end - p)),
            )
            method = AttributionMethod.PINPOINT
        else:
            explicit = [
                c
                for c in same_paragraph
                if not patterns.SENTENCE_BREAK.search(text[slice(*_between(c.span, quote.span))])
            ]
            if explicit:
                chosen = min(explicit, key=lambda c: _gap(c.span, quote.span))
                method = AttributionMethod.EXPLICIT
            else:
                near = [
                    c for c in same_paragraph if _gap(c.span, quote.span) <= PROXIMITY_MAX_CHARS
                ]
                if near:
                    chosen = min(near, key=lambda c: _gap(c.span, quote.span))
                    method = AttributionMethod.PROXIMITY

        out.append(
            quote.model_copy(
                update={
                    "attributed_cluster_ordinal": chosen.ordinal if chosen else None,
                    "attribution_method": method,
                    # Kept even when no cluster could be tied to the quote: the
                    # paragraph number is still evidence worth showing a user.
                    "pinpoint_paragraph": pinpoint_paragraph,
                }
            )
        )
    return out
