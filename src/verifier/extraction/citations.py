"""Citation extraction and clustering.

Extraction turns raw AI output into ``ExtractedCitation`` objects. Clustering then
collapses the several *forms* of one reference into a single ``CitationCluster``, which
is the unit everything downstream resolves.

Why clustering matters more than it looks: a report citation is not resolvable at all
(F7 -- eLitigation's index is full-text, so searching "[2007] 4 SLR(R) 100" returns the
cases that *cite* it, never the case itself). On its own it can only ever be a WARN.
But in real legal writing it almost never travels alone:

    Spandeck Engineering (S) Pte Ltd v DSTA [2007] 4 SLR(R) 100; [2007] SGCA 37

That is three extracted citations and one actual reference. Cluster them, prefer the
neutral citation, and the report citation is verified for free.
"""

from __future__ import annotations

import re

from verifier.contracts.citations import CitationCluster, ExtractedCitation, Span
from verifier.contracts.enums import CitationType
from verifier.extraction import patterns

#: Maximum gap, in characters, between two citation forms for them to be treated as one
#: reference. ~80 chars is about one long case name plus punctuation -- wide enough for
#: "A v B [2007] 4 SLR(R) 100; [2007] SGCA 37", narrow enough that the next sentence's
#: citation does not get absorbed.
CLUSTER_WINDOW_CHARS = 80


# ---------------------------------------------------------------------------
# Case-name cleanup: the precision layer
# ---------------------------------------------------------------------------


def _tokens(party: str) -> list[str]:
    return party.split()


def _clean_party(party: str, *, leading: bool) -> str | None:
    """Trim sentence filler off a party and reject it if what is left is not a name.

    ``leading`` is True for the left-hand party (filler accumulates at its START, e.g.
    "In Spandeck ... v ...") and False for the right-hand party (filler accumulates at
    its END, e.g. "... v Defence Science & Technology Agency The court held").

    Returns None when the side is not a plausible party. Returning None is the safe
    outcome: it means we never search for this phrase, so it can never produce the
    zero-hit result that we read as evidence of fabrication.
    """
    # Trim filler from the outer edge. The greedy Title-Case run absorbs a capitalised
    # sentence opener on the left ("In Spandeck ...") and the next sentence's opener on
    # the right ("... Agency The Court then held"). Both stopword sets are trimmed,
    # because a party never begins or ends with one and leaving them in changes the
    # phrase we search for -- which changes a hit into a zero-hit.
    trimmable = patterns.PARTY_EDGE_FILLER | patterns.PARTY_STOPWORDS
    toks = _tokens(party)
    while toks:
        edge = toks[0] if leading else toks[-1]
        if edge.lower().strip(".,;:") in trimmable:
            toks = toks[1:] if leading else toks[:-1]
            continue
        break
    if not toks:
        return None

    lowered = [t.lower().strip(".,;:") for t in toks]

    if len(toks) < patterns.MIN_PARTY_TOKENS:
        # One-token sides are admitted only for the handful of institutional parties
        # that are always written that way. F6 verified "Tan Cheng Bock v AG" resolving
        # at rank 1, so the rule has to let AG through -- and has to do so before the
        # length test below, which "AG" would otherwise fail on two characters.
        return " ".join(toks) if lowered[0] in patterns.SINGLE_TOKEN_PARTIES else None

    # Every party needs at least one substantive word. "A B v C D" is not a case name.
    if not any(len(t) > 2 for t in lowered):
        return None

    return " ".join(toks)


def _case_name_spans(text: str, blocked: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """Yield (start, end, case_name) for every case name we are confident about."""
    out: list[tuple[int, int, str]] = []
    for match in patterns.CASE_NAME.finditer(text):
        raw_left, raw_right = match.group("left"), match.group("right")
        left = _clean_party(raw_left, leading=True)
        right = _clean_party(raw_right, leading=False)
        if left is None or right is None:
            continue

        # Recompute the span over the *cleaned* text so the highlight, the raw_text and
        # the search phrase are all the same string.
        #
        # Both sides need the containment guard. ``_clean_party`` rejoins its tokens on
        # single spaces, so a party name that wrapped across a newline -- which is how
        # markdown renders any long case name -- is NOT a substring of the text it came
        # from, and ``index`` raises. That exception propagates out of L0 and the
        # orchestrator degrades the whole run to an empty extraction: no citations, no
        # propositions, nothing checked, on exactly the well-formatted answers this
        # system is meant to verify.
        start = match.start("left") + raw_left.index(left) if left in raw_left else match.start()
        end = (
            match.start("right") + raw_right.index(right) + len(right)
            if right in raw_right
            else match.end()
        )
        if any(bs < end and start < be for bs, be in blocked):
            continue
        sep = match.group("sep")
        out.append((start, end, f"{left} {sep} {right}"))
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_citations(text: str) -> list[ExtractedCitation]:
    """Extract every citation form from ``text``, ordered by position.

    ``ordinal`` is the 0-based index into the returned list and is stable for a given
    input, so findings can point at a citation by number.
    """
    found: list[tuple[int, int, ExtractedCitation]] = []
    blocked: list[tuple[int, int]] = []

    for match in patterns.URL.finditer(text):
        blocked.append((match.start(), match.end()))
        found.append(
            (
                match.start(),
                match.end(),
                ExtractedCitation(
                    ordinal=0,
                    raw_text=match.group(0),
                    citation_type=CitationType.URL,
                    span=Span(start=match.start(), end=match.end()),
                    url=match.group(0),
                ),
            )
        )

    for match in patterns.NEUTRAL_CITATION.finditer(text):
        if any(bs < match.end() and match.start() < be for bs, be in blocked):
            continue
        found.append(
            (
                match.start(),
                match.end(),
                ExtractedCitation(
                    ordinal=0,
                    raw_text=match.group(0),
                    citation_type=CitationType.NEUTRAL,
                    span=Span(start=match.start(), end=match.end()),
                    court=match.group("court"),
                    year=int(match.group("year")),
                    number=int(match.group("number")),
                ),
            )
        )

    neutral_spans = [(s, e) for s, e, c in found if c.citation_type is CitationType.NEUTRAL]
    for match in patterns.REPORT_CITATION.finditer(text):
        if any(bs < match.end() and match.start() < be for bs, be in blocked + neutral_spans):
            continue
        found.append(
            (
                match.start(),
                match.end(),
                ExtractedCitation(
                    ordinal=0,
                    raw_text=match.group(0),
                    citation_type=CitationType.REPORT,
                    span=Span(start=match.start(), end=match.end()),
                    year=int(match.group("year")),
                ),
            )
        )

    citation_spans = [(s, e) for s, e, _ in found]
    for start, end, name in _case_name_spans(text, citation_spans):
        found.append(
            (
                start,
                end,
                ExtractedCitation(
                    ordinal=0,
                    raw_text=name,
                    citation_type=CitationType.CASE_NAME,
                    span=Span(start=start, end=end),
                    case_name=name,
                ),
            )
        )

    found.sort(key=lambda item: (item[0], item[1]))
    return [c.model_copy(update={"ordinal": i}) for i, (_, _, c) in enumerate(found)]


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _would_conflict(members: list[ExtractedCitation], candidate: ExtractedCitation) -> bool:
    """True when adding ``candidate`` would merge two different references.

    A cluster may hold at most one neutral citation and at most one case name. Two
    neutral citations sitting 20 characters apart are a string cite ("[2007] SGCA 37;
    [2008] SGCA 4"), not one reference, and merging them would silently drop a citation
    from verification. Multiple report citations are allowed, because parallel report
    series for a single case are common and none of them is resolvable anyway.
    """
    if candidate.citation_type is CitationType.NEUTRAL:
        return any(m.citation_type is CitationType.NEUTRAL for m in members)
    if candidate.citation_type is CitationType.CASE_NAME:
        return any(m.citation_type is CitationType.CASE_NAME for m in members)
    if candidate.citation_type is CitationType.URL:
        return any(m.citation_type is CitationType.URL for m in members)
    return False


def cluster_citations(
    citations: list[ExtractedCitation],
    *,
    window: int = CLUSTER_WINDOW_CHARS,
    text: str | None = None,
) -> list[CitationCluster]:
    """Group citation forms that refer to the same case into clusters.

    Pass ``text`` to enable the sentence-boundary guard. A single reference is always
    written inside one sentence; two citations separated by a full stop are two
    references even when they sit close together ("... [2007] SGCA 37. See also
    https://..."). Without the guard, proximity alone silently merges them and one of
    them stops being verified.
    """
    ordered = sorted(citations, key=lambda c: (c.span.start, c.span.end))
    groups: list[list[ExtractedCitation]] = []

    for citation in ordered:
        if groups:
            current = groups[-1]
            reach = max(m.span.end for m in current)
            gap = text[reach : citation.span.start] if text is not None else ""
            crosses_sentence = bool(patterns.SENTENCE_BREAK.search(gap))
            if (
                citation.span.start - reach <= window
                and not crosses_sentence
                and not _would_conflict(current, citation)
            ):
                current.append(citation)
                continue
        groups.append([citation])

    clusters: list[CitationCluster] = []
    for index, members in enumerate(groups):
        case_name = next(
            (m.case_name for m in members if m.citation_type is CitationType.CASE_NAME), None
        )
        # Stamping the sibling case name onto every member is what enables the
        # cross-check that catches a real citation attached to the wrong case.
        stamped = tuple(
            m.model_copy(update={"case_name": m.case_name or case_name}) for m in members
        )
        clusters.append(
            CitationCluster(
                ordinal=index,
                members=stamped,
                span=Span(
                    start=min(m.span.start for m in stamped),
                    end=max(m.span.end for m in stamped),
                ),
            )
        )
    return clusters


def extract_clusters(text: str, *, window: int = CLUSTER_WINDOW_CHARS) -> list[CitationCluster]:
    """Convenience: extract then cluster."""
    return cluster_citations(extract_citations(text), window=window, text=text)


def search_phrase(cluster: CitationCluster) -> str | None:
    """The phrase to send to a full-text case-name search, or None if there isn't one."""
    for member in cluster.members:
        if member.citation_type is CitationType.CASE_NAME and member.case_name:
            return re.sub(r"\s+", " ", member.case_name).strip()
    return None
