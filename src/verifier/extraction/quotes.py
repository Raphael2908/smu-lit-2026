"""Quote extraction.

Only text that the AI output *presents as a direct quotation* belongs here, and that is
the whole point of the module.

Part 3 of docs/03-findings.md measures why, under the matcher L1 actually uses
(``rapidfuzz.partial_ratio``, against Spandeck paragraph [115]): an honest paraphrase
scores **49.7** and an invented sentence scores **46.1**. A 3.6-point gap is noise, and
both sit far below the 75 FAIL threshold. Lexical similarity simply cannot tell a
faithful restatement from plausible fiction, so pointing L1 at un-delimited prose does
not add a weak signal -- it fails correct legal writing on what amounts to a coin flip.

L1 may therefore only ever score spans that were *presented as a direct quotation*.
That is why ``ExtractedQuote.delimiter`` is a required field rather than a nice-to-have,
and why this module never emits a quote it cannot name the delimiter for.

Paraphrase is not ignored by the system; it is L3's question, scored by retrieval margin
instead of string similarity.
"""

from __future__ import annotations

from verifier.contracts.citations import ExtractedQuote, Span
from verifier.extraction import patterns
from verifier.settings import settings

#: Regexes tried in order. Earlier entries win when spans overlap, so an inline quote
#: nested inside a blockquote is reported once, as the blockquote.
_INLINE_PATTERNS: tuple[tuple[str, object], ...] = (
    ("“", patterns.CURLY_DOUBLE_QUOTE),
    ('"', patterns.STRAIGHT_DOUBLE_QUOTE),
    ("'", patterns.CURLY_SINGLE_QUOTE),
    ("'", patterns.STRAIGHT_SINGLE_QUOTE),
)


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s < end and start < e for s, e in spans)


def _is_substantive(body: str, min_chars: int) -> bool:
    """Reject short spans and spans that are only punctuation.

    The length floor is ``MIN_QUOTE_CHARS`` (40). A 15-character "quote" is a turn of
    phrase, not an appeal to what a judgment said, and treating one as a quotation only
    adds noise to the units built on top of it.
    """
    stripped = body.strip()
    return len(stripped) >= min_chars and any(ch.isalpha() for ch in stripped)


def extract_quotes(text: str, *, min_chars: int | None = None) -> list[ExtractedQuote]:
    """Extract every span presented as a direct quotation, ordered by position.

    ``span`` is the region to highlight in the AI output. For inline quotes it is the
    body between the delimiters, so ``text[span.start:span.end] == quote.text``. For a
    markdown blockquote the span covers the whole ">"-prefixed run while ``text`` is the
    run with its markers stripped -- the marker characters are presentation, and feeding
    them to a fuzzy matcher would depress every score.
    """
    floor = settings.MIN_QUOTE_CHARS if min_chars is None else min_chars
    taken: list[tuple[int, int]] = []
    collected: list[tuple[int, int, str, str]] = []  # start, end, text, delimiter

    for match in patterns.BLOCKQUOTE.finditer(text):
        start, end = match.start(), match.end()
        body = patterns.BLOCKQUOTE_MARKER.sub("", match.group(0)).strip()
        if not _is_substantive(body, floor):
            continue
        taken.append((start, end))
        collected.append((start, end, body, "blockquote"))

    for delimiter, pattern in _INLINE_PATTERNS:
        for match in pattern.finditer(text):  # type: ignore[attr-defined]
            start, end = match.start("body"), match.end("body")
            if _overlaps(taken, match.start(), match.end()):
                continue
            body = match.group("body")
            if not _is_substantive(body, floor):
                continue
            taken.append((match.start(), match.end()))
            collected.append((start, end, body, delimiter))

    collected.sort(key=lambda item: (item[0], item[1]))
    return [
        ExtractedQuote(
            ordinal=index,
            text=body,
            span=Span(start=start, end=end),
            delimiter=delimiter,
        )
        for index, (start, end, body, delimiter) in enumerate(collected)
    ]
