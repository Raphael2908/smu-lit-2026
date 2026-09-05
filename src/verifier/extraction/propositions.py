"""L1a extraction: which sentences assert law, and what authority stands behind them.

This module answers the question that comes *before* "does the citation exist": an
output can be entirely free of fabricated citations by citing nothing whatsoever. L1b
and L1c only ever look at authority the output actually offered, so without this an
answer that states the law from memory, confidently and with no support at all, passes
the citation-integrity layer untouched.

TWO OPPOSITE BIASES, ON PURPOSE.

**Classification is narrow.** A sentence qualifies only on an explicit legal-assertion
cue -- a holding verb with a judicial subject, a statement of a legal test, an appeal
to settled law, a statutory obligation. Framing, hedges, questions, conditionals,
applications to the user's own facts and quoted material are all excluded. This is the
same doctrine that governs ``patterns.py``: a classifier that fires on ordinary prose
does not merely over-report, it manufactures uncited-claim findings against correct
legal writing.

**Coverage is generous.** Citation placement in prose has no fixed structure. Authority
may precede its proposition, follow it, sit in a parenthetical, or appear once at the
head of a paragraph that then discusses it for five sentences. So anything in the same
scope counts, at any distance (``CARRIED``). The consequence is deliberate: where the
attribution is ambiguous we call an uncited claim cited, never the reverse.

That asymmetry is what lets L1a carry a FAIL at all. The FAIL is not an attribution
judgement -- it is a count over the whole output (``ExtractionResult.authority_count``
is zero), so there is nothing in it to be wrong about beyond whether the text contains
a citation. Per-proposition findings, where the hard judgement lives, only ever WARN.
"""

from __future__ import annotations

import re

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedProposition,
    ExtractedQuote,
    Span,
    StatuteReference,
)
from verifier.contracts.enums import AttributionMethod, AuthorityKind, PropositionKind
from verifier.extraction import patterns

__all__ = [
    "extract_propositions",
    "extract_statutes",
    "scope_spans",
    "sentence_spans",
    "subsequent_references",
]

#: Same-scope proximity budget, matching ``attribution.PROXIMITY_MAX_CHARS``. A quote
#: and a proposition are attributed by the same reasoning, so they use the same number.
PROXIMITY_MAX_CHARS = 400

#: Sentences shorter than this are fragments, headings or list stubs. Classifying one
#: costs precision and gains nothing: a five-word sentence rarely asserts a legal rule.
MIN_PROPOSITION_CHARS = 30

# --- statutes ----------------------------------------------------------------------


def extract_statutes(text: str) -> list[StatuteReference]:
    """Every statutory reference in the output, specific ones and vague ones alike.

    A *specific* reference ("s 20 of the Building Control Act", "Cap 29") is authority:
    the proposition it supports rests on something a reader can look up. A *vague* one
    ("under the Act") is not -- it points at something that must have been named
    earlier, and an output that never names it has supported nothing.

    These are deliberately not ``CitationCluster``s. A cluster is something L1b tries to
    resolve against the judgment corpus; a statute is not in that corpus, so making one
    a cluster would emit CITATION_UNVERIFIED for every correctly cited section.
    """
    found: list[tuple[int, int, dict[str, str | None]]] = []

    for match in patterns.NAMED_LEGISLATION.finditer(text):
        act = match.group("act")
        # A named Act is only a reference when it is actually a title. Requiring the
        # leading token to be capitalised is what keeps "the act of signing" out.
        if not act or not act[0].isupper():
            continue
        found.append((match.start(), match.end(), {"act": _squash(act), "section": None}))

    for match in patterns.CHAPTER_REFERENCE.finditer(text):
        found.append((match.start(), match.end(), {"chapter": match.group("chapter")}))

    for match in patterns.SECTION_REFERENCE.finditer(text):
        keyword = match.group("kw").lower()
        # 'para 12' / 'art 5' inside a judgment pinpoint is not a statutory section, and
        # the pinpoint machinery already owns it.
        if keyword.startswith("para"):
            continue
        found.append((match.start(), match.end(), {"section": match.group("number")}))

    for match in patterns.ORDER_RULE_REFERENCE.finditer(text):
        section = f"O {match.group('order')} r {match.group('rule')}"
        found.append((match.start(), match.end(), {"section": section}))

    # "2007 Rev Ed" and "Act 19 of 2016" are PARTS of a citation, never one on their
    # own -- the guide writes them inside the statute's parentheses (paras 2-2.1.2.1
    # and 2-2.1.2.2). They are collected so the merge absorbs them into the reference
    # they belong to, and ``_anchored`` then drops any that stand alone.
    for match in patterns.REVISED_EDITION.finditer(text):
        found.append((match.start(), match.end(), {"_part": "rev_ed"}))
    for match in patterns.ACT_NUMBER.finditer(text):
        found.append((match.start(), match.end(), {"_part": "act_no"}))

    merged = _anchored(_merge_adjacent(text, found))

    statutes: list[StatuteReference] = []
    for ordinal, (start, end, fields) in enumerate(merged):
        statutes.append(
            StatuteReference(
                ordinal=ordinal,
                raw_text=text[start:end],
                span=Span(start=start, end=end),
                act=fields.get("act"),
                section=fields.get("section"),
                chapter=fields.get("chapter"),
                specific=True,
            )
        )

    taken = [(s.span.start, s.span.end) for s in statutes]
    for match in patterns.VAGUE_LEGISLATION.finditer(text):
        if _overlaps(taken, match.start(), match.end()):
            continue
        statutes.append(
            StatuteReference(
                ordinal=len(statutes),
                raw_text=match.group(0),
                span=Span(start=match.start(), end=match.end()),
                specific=False,
            )
        )

    statutes.sort(key=lambda s: (s.span.start, s.span.end))
    return [s.model_copy(update={"ordinal": index}) for index, s in enumerate(statutes)]


def _merge_adjacent(
    text: str, found: list[tuple[int, int, dict[str, str | None]]]
) -> list[tuple[int, int, dict[str, str | None]]]:
    """Fold "s 20 of the Building Control Act (Cap 29)" into ONE reference.

    Three regexes match that string. Reporting three statutes would treble the
    authority count, and ``authority_count`` is what L1a's FAIL turns on -- so the
    count has to mean "distinct pieces of authority", not "regex hits".
    """
    if not found:
        return []
    found.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int, dict[str, str | None]]] = [found[0]]
    for start, end, fields in found[1:]:
        last_start, last_end, last_fields = merged[-1]
        gap = text[last_end:start] if start >= last_end else ""
        # Only connective filler may sit between two halves of one reference:
        # "s 20 of the Building Control Act (Cap 29)" is one, "s 20 and the Evidence
        # Act" is two.
        if start <= last_end or _CONNECTIVE_GAP.fullmatch(gap):
            combined = {**last_fields}
            for key, value in fields.items():
                if combined.get(key) is None:
                    combined[key] = value
            merged[-1] = (last_start, _close_paren(text, max(last_end, end)), combined)
        else:
            merged.append((start, end, fields))
    return merged


def _anchored(
    merged: list[tuple[int, int, dict[str, str | None]]],
) -> list[tuple[int, int, dict[str, str | None]]]:
    """Drop references that are only a fragment of one.

    "2007 Rev Ed" or "Act 19 of 2016" standing alone -- with no Act, section or chapter
    beside it to attach to -- names no legislation. Counting one as authority would let
    an answer clear L1a by writing an edition marker.
    """
    anchors = ("act", "section", "chapter")
    return [item for item in merged if any(item[2].get(key) for key in anchors)]


#: What may separate two halves of a single statutory reference. The comma matters:
#: the guide writes "(Cap 322, 2007 Rev Ed)" as ONE citation (para 2-2.1.2.2).
#: "Parts of statutes should be cited from the largest part to the smallest" (para
#: 2-2.1.2.1), so "... Act 2016, The Schedule, Pt 1" is one reference with a pinpoint,
#: not two authorities.
_CONNECTIVE_GAP = re.compile(
    r"[\s,()]*(?:(?:of|under|in|to|the|read\s+with"
    r"|First|Second|Third|Fourth|Fifth|Sixth|Schedule|Schedules)\s*)*[\s,()]*"
)


def _close_paren(text: str, end: int) -> int:
    """Extend a span past a closing bracket it opened.

    "(Cap 29)" is matched as "Cap 29", so the merged reference would otherwise be
    displayed to the user as "... Act (Cap 29" -- ragged text in a panel whose whole
    job is to look more careful than the thing it audits.
    """
    if text[:end].count("(") > text[:end].count(")") and text[end : end + 1] == ")":
        return end + 1
    return end


def _squash(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s < end and start < e for s, e in spans)


# --- segmentation ------------------------------------------------------------------


def scope_spans(text: str) -> list[tuple[int, int]]:
    """Attribution scopes: the regions within which a citation governs.

    A scope is a paragraph, except that every list item and heading opens a new one --
    and a heading closes immediately, so it never lends its citation to the prose
    beneath it.

    Markdown lists carry no blank lines, so paragraph splitting alone would make one
    scope of a ten-bullet answer and let a citation in the first bullet clear an
    unrelated assertion in the tenth. Neighbouring bullets are still reachable by the
    proximity rule; only the unlimited-distance carry-forward stops at the item edge.
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    cursor = 0

    def flush(end: int) -> None:
        nonlocal start
        if start is not None and end > start:
            spans.append((start, end))
        start = None

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        heading = bool(patterns.MARKDOWN_HEADING.match(line))
        if not stripped or heading or patterns.LIST_MARKER.match(line):
            flush(cursor)
        if stripped:
            if start is None:
                start = cursor
            if heading:
                flush(cursor + len(line))
        cursor += len(line)

    flush(cursor)
    return spans


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentence spans over the ORIGINAL string, so findings can highlight exact text.

    Sentences are found *within blocks*, never within lines. Prose is routinely hard
    wrapped -- markdown breaks any long sentence across lines -- so splitting per line
    would cut "The court held / that X" in two and leave both halves below the length
    floor, unclassified. The layer would then report nothing on precisely the
    well-formatted answers it exists to check. Blocks end at a blank line, a list marker
    or a heading, which is the same boundary a citation's scope respects.

    ``semantic.chunking.split_sentences`` returns strings and lives on the embedding
    path; L1 is deterministic and must not import it. The offsets matter here anyway:
    a proposition finding has to point the panel at the sentence it is about.
    """
    spans: list[tuple[int, int]] = []
    for block_start, block_end in scope_spans(text):
        block = text[block_start:block_end]
        # Drop a leading "- " / "1. " so a bulleted assertion reads as a plain sentence.
        marker = patterns.LIST_MARKER.match(block)
        cursor = block_start + (marker.end() if marker else 0)
        for match in patterns.SENTENCE_BREAK.finditer(block):
            end = block_start + match.start() + 1  # keep the terminal punctuation
            if end > cursor:
                spans.append((cursor, end))
            cursor = block_start + match.end()
        if block_end > cursor:
            spans.append((cursor, block_end))
    return [(s, e) for s, e in spans if text[s:e].strip()]


# --- classification ----------------------------------------------------------------


def _classify(sentence: str) -> tuple[PropositionKind, str] | None:
    """Does this sentence assert law? Returns the kind and the cue that decided it.

    Exclusions are checked FIRST and win outright. A sentence can carry a holding verb
    and still be a hypothesis ("the court would have held that ..."), an application to
    the user's facts, or a disclaimer -- and in each of those cases demanding a citation
    is wrong. Precision-first means the classifier stays silent when the signals fight.
    """
    if patterns.MARKDOWN_HEADING.match(sentence):
        return None
    body = patterns.LIST_MARKER.sub("", sentence).strip()
    if len(body) < MIN_PROPOSITION_CHARS or not any(ch.isalpha() for ch in body):
        return None
    if body.rstrip().endswith("?"):
        return None
    for exclusion in (
        patterns.META_CUE,
        patterns.DISCLAIMER_CUE,
        patterns.APPLICATION_CUE,
        patterns.SPECULATIVE_CUE,
        patterns.CONDITIONAL_OPENER,
    ):
        if exclusion.search(body):
            return None

    # ESTABLISHED is tested before HOLDING because it is the more specific reading of
    # an overlapping string: "it is well established that" contains a holding verb, but
    # it is an appeal to settled law, not a report of what a court did.
    established = patterns.ESTABLISHED_CUE.search(body)
    if established is not None:
        return PropositionKind.ESTABLISHED, established.group(0)

    # A holding needs either a judicial subject or a "that"-complement. "Held" alone is
    # ambiguous English; "the court held" and "held that" are not.
    holding = patterns.HOLDING_THAT.search(body)
    if holding is None and patterns.JUDICIAL_ACTOR.search(body):
        holding = patterns.HOLDING_VERB.search(body)
    if holding is not None:
        return PropositionKind.HOLDING, holding.group(0)

    test = patterns.LEGAL_TEST_CUE.search(body)
    if test is not None:
        return PropositionKind.LEGAL_TEST, test.group(0)

    statutory = patterns.VAGUE_LEGISLATION.search(body) or patterns.SECTION_REFERENCE.search(body)
    if statutory is not None:
        return PropositionKind.STATUTE, statutory.group(0)

    return None


# --- the entry point ---------------------------------------------------------------


def extract_propositions(
    text: str,
    clusters: list[CitationCluster],
    statutes: list[StatuteReference],
    quotes: list[ExtractedQuote] | None = None,
) -> list[ExtractedProposition]:
    """Every sentence that asserts law, with whatever authority covers it.

    Coverage ladder, descending confidence: EXPLICIT (authority inside the sentence) ->
    PROXIMITY (same scope, within 400 characters either way) -> CARRIED (anywhere
    earlier in the same scope) -> NONE.
    """
    # Classify against a copy with quotations blanked out. A cue inside a quotation is
    # the COURT's words, not the model's assertion: "the court said: 'it is well
    # established that ...'" must not be read as the answer itself appealing to settled
    # law. Quoted text is L1c's question, checked against the source it came from.
    masked = _mask(text, quotes or [])
    scopes = scope_spans(text)
    authorities = _authorities(clusters, statutes, subsequent_references(text, clusters))

    propositions: list[ExtractedProposition] = []
    for start, end in sentence_spans(text):
        sentence = text[start:end]
        classified = _classify(masked[start:end])
        if classified is None:
            continue
        kind, cue = classified
        span = Span(start=start, end=end)
        method, authority = _cover(span, scopes, authorities)
        propositions.append(
            ExtractedProposition(
                ordinal=len(propositions),
                text=sentence.strip(),
                span=span,
                kind=kind,
                cue=_squash(cue),
                authority=authority[0] if authority else AuthorityKind.NONE,
                attribution_method=method,
                attributed_cluster_ordinal=(
                    authority[1] if authority and authority[0] is AuthorityKind.CITATION else None
                ),
                attributed_statute_ordinal=(
                    authority[1] if authority and authority[0] is AuthorityKind.STATUTE else None
                ),
            )
        )
    return propositions


def subsequent_references(
    text: str, clusters: list[CitationCluster]
) -> list[tuple[AuthorityKind, int, Span]]:
    """Short-title and ``supra`` references back to a citation given earlier.

    SLR style cites a case in full once and refers back to it thereafter (Style Guide
    2021, paras 2-1.1.1 and 2-1.5)::

        1   ... The case of ANJ v ANK [2015] 4 SLR 1043 ("ANJ") stands for ...
        8   As was discussed in ANJ ([1] supra) ...
        20  This point was raised in ANJ at [32].

    Paragraphs 8 and 20 are properly cited. Counting only full citations would read
    them as unsupported assertions -- penalising precisely the citation style the
    sponsor's own house guide mandates, which is the largest single source of false
    positives available to this layer.

    A short title is only honoured if the output DEFINED it, in parentheses, after a
    citation. That keeps an arbitrary capitalised word from being read as a reference.
    """
    out: list[tuple[AuthorityKind, int, Span]] = []
    if not clusters:
        return out
    by_start = sorted(clusters, key=lambda c: c.span.start)

    def preceding_cluster(position: int) -> CitationCluster | None:
        earlier = [c for c in by_start if c.span.start <= position]
        return earlier[-1] if earlier else None

    for match in patterns.SHORT_TITLE_DEFINITION.finditer(text):
        title = match.group("title").strip()
        if not title or title.lower() in patterns.SHORT_TITLE_STOPWORDS:
            continue
        # The definition must sit just after the citation it names, per the guide's
        # "defined after the first mention of the full citation".
        owner = preceding_cluster(match.start())
        if owner is None or match.start() - owner.span.end > _DEFINITION_MAX_GAP:
            continue
        for mention in re.finditer(rf"\b{re.escape(title)}\b", text):
            if mention.start() <= match.end():
                continue  # the defining occurrence itself, and the citation before it
            out.append(
                (
                    AuthorityKind.CITATION,
                    owner.ordinal,
                    Span(start=mention.start(), end=mention.end()),
                )
            )

    for match in patterns.SUPRA_REFERENCE.finditer(text):
        owner = preceding_cluster(match.start())
        if owner is None:
            continue
        out.append(
            (AuthorityKind.CITATION, owner.ordinal, Span(start=match.start(), end=match.end()))
        )

    return out


#: How far after a citation a short-title definition may sit and still belong to it.
#: Generous enough for "... [2015] 4 SLR 1043 at [17] ("ANJ")", tight enough that a
#: quoted phrase a sentence later is not mistaken for one.
_DEFINITION_MAX_GAP = 80


def _authorities(
    clusters: list[CitationCluster],
    statutes: list[StatuteReference],
    subsequent: list[tuple[AuthorityKind, int, Span]] | None = None,
) -> list[tuple[AuthorityKind, int, Span]]:
    """Everything that can support a proposition, ordered by position.

    Vague statutory references are excluded: "the Act" cannot support the sentence it
    appears in, because it names nothing.
    """
    items: list[tuple[AuthorityKind, int, Span]] = [
        (AuthorityKind.CITATION, c.ordinal, c.span) for c in clusters
    ]
    items += [(AuthorityKind.STATUTE, s.ordinal, s.span) for s in statutes if s.specific]
    items += list(subsequent or [])
    items.sort(key=lambda item: (item[2].start, item[2].end))
    return items


def _cover(
    span: Span,
    scopes: list[tuple[int, int]],
    authorities: list[tuple[AuthorityKind, int, Span]],
) -> tuple[AttributionMethod, tuple[AuthorityKind, int] | None]:
    if not authorities:
        return AttributionMethod.NONE, None

    inside = [a for a in authorities if a[2].start < span.end and span.start < a[2].end]
    if inside:
        best = min(inside, key=lambda a: abs(a[2].start - span.start))
        return AttributionMethod.EXPLICIT, (best[0], best[1])

    scope = _containing(scopes, span.start)
    if scope is None:
        return AttributionMethod.NONE, None
    in_scope = [a for a in authorities if scope[0] <= a[2].start < scope[1]]
    if not in_scope:
        return AttributionMethod.NONE, None

    near = [a for a in in_scope if _gap(span, a[2]) <= PROXIMITY_MAX_CHARS]
    if near:
        best = min(near, key=lambda a: _gap(span, a[2]))
        return AttributionMethod.PROXIMITY, (best[0], best[1])

    # Carry-forward: a citation given earlier in this scope governs what follows it.
    # Legal writing cites once and then discusses; without this every sentence after
    # the first would read as uncited, and the layer would be pure noise.
    earlier = [a for a in in_scope if a[2].end <= span.start]
    if earlier:
        best = max(earlier, key=lambda a: a[2].end)
        return AttributionMethod.CARRIED, (best[0], best[1])

    return AttributionMethod.NONE, None


def _containing(spans: list[tuple[int, int]], position: int) -> tuple[int, int] | None:
    for start, end in spans:
        if start <= position < end:
            return (start, end)
    return None


def _gap(a: Span, b: Span) -> int:
    if a.end <= b.start:
        return b.start - a.end
    if b.end <= a.start:
        return a.start - b.end
    return 0


def _mask(text: str, quotes: list[ExtractedQuote]) -> str:
    """``text`` with every quotation replaced by spaces, offsets preserved.

    Blanking rather than removing keeps every span valid against the original string,
    so a finding still highlights the right characters in the AI output.
    """
    if not quotes:
        return text
    chars = list(text)
    for quote in quotes:
        for index in range(quote.span.start, min(quote.span.end, len(chars))):
            chars[index] = " "
    return "".join(chars)
