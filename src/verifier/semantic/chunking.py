"""Splitting documents and AI outputs into embeddable units.

Chunking a source judgment is MANDATORY, not an optimisation. F9: a judgment body is
~83.8k chars ~= 21K tokens against voyage-law-2's 16K context window, so an unchunked
document simply cannot be embedded -- it would be silently truncated, and every
similarity computed against it would be against the first two-thirds of the case.

Two chunkers live here because the two sides of a retrieval comparison have different
natural units:

* the SOURCE side has real structure (eLitigation marks numbered paragraphs as
  ``Judg-1`` and block quotes as ``Judg-Quote-1``, F5), so we merge whole paragraphs
  and never cut mid-idea;
* the OUTPUT side has none, so we ask the summariser for atomic claims and fall back
  to deterministic sentence windows when that is unavailable or unparseable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from verifier.contracts.citations import Span
from verifier.contracts.documents import Paragraph, SourceDocument
from verifier.contracts.enums import ChunkKind
from verifier.providers.base import Summariser
from verifier.settings import settings

#: Characters per token. A heuristic, NOT a tokeniser: it is deliberately crude so that
#: chunking never depends on a vendor tokeniser being importable, and deliberately
#: conservative (real English averages ~4.0-4.7 chars/token, legal prose with citations
#: and paragraph markers sits at the low end) so the estimate over-counts rather than
#: under-counts. Over-counting shrinks chunks; under-counting overflows the context
#: window, which is the failure we cannot detect at runtime.
CHARS_PER_TOKEN = 4

#: Anything presented as running text of the judgment. Headings are folded into
#: ``heading_path`` by the parser, so they are context rather than content.
_CONTENT_KINDS = (ChunkKind.BODY, ChunkKind.QUOTE)

# Sentence boundary: terminal punctuation followed by whitespace and something that
# looks like the start of a new sentence. The lookbehind excludes the abbreviations that
# actually occur in Singapore judgments -- "v.", "Ltd.", "No.", "para." -- because
# splitting on those produces fragments that embed as noise.
_ABBREVIATIONS = (
    "v",
    "vs",
    "ltd",
    "pte",
    "co",
    "no",
    "nos",
    "para",
    "paras",
    "art",
    "cf",
    "ed",
    "eds",
    "jj",
    "j",
    "ca",
    "hc",
    "mr",
    "mrs",
    "ms",
    "dr",
    "st",
    "ss",
    "s",
    "r",
    "ors",
    "anor",
    "etc",
    "ie",
    "eg",
)
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])["\')\]]?\s+(?=[A-Z“"(\[])')
_TRAILING_WORD = re.compile(r"([A-Za-z]+)\.$")


@dataclass(frozen=True)
class RawChunk:
    """A chunk before contextualisation.

    Deliberately *not* :class:`~verifier.contracts.documents.Chunk`: that contract
    requires ``embed_input`` and its hash, which only exist once the document summary
    and heading path have been prefixed. Keeping the two apart means the cache key is
    always derived from the exact string that was sent to the model, never from the
    bare text. ``heading_path`` lives here and not on ``Chunk`` for the same reason --
    it is an input to the embed string, not a property of the embedded unit.
    """

    ordinal: int
    kind: ChunkKind
    text: str
    heading_path: tuple[str, ...] = ()
    paragraph_from: int | None = None
    paragraph_to: int | None = None
    #: Offsets into the AI output. Present for output chunks (L3 uses them to decide
    #: which claims a citation is responsible for), absent for source chunks.
    span: Span | None = None
    #: Provenance of the split, surfaced in ``LayerResult.detail`` so a reviewer can see
    #: whether claims came from the model or from the deterministic fallback.
    strategy: str = "paragraph"

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass
class _Accumulator:
    paragraphs: list[Paragraph] = field(default_factory=list)
    chars: int = 0

    def clear(self) -> None:
        self.paragraphs = []
        self.chars = 0


def estimate_tokens(text: str) -> int:
    """Approximate token count at ~4 chars/token.

    APPROXIMATE BY DESIGN. Exact counts would need the vendor tokeniser, which would
    make offline chunking depend on a network-installed model. The number is only ever
    used to decide where to cut, and every cut is verified against a hard character
    budget afterwards, so an error here costs chunk evenness, never correctness.
    """
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _budget_chars(target_tokens: int) -> int:
    return max(1, target_tokens * CHARS_PER_TOKEN)


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences, protecting legal abbreviations."""
    text = text.strip()
    if not text:
        return []
    pieces = _SENTENCE_SPLIT.split(text)
    merged: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if merged:
            trailing = _TRAILING_WORD.search(merged[-1])
            if trailing and trailing.group(1).lower() in _ABBREVIATIONS:
                merged[-1] = f"{merged[-1]} {piece}"
                continue
        merged.append(piece)
    return merged


def _split_oversized(text: str, budget_chars: int, overlap_chars: int) -> list[str]:
    """Cut a single over-long unit down to size.

    Only reached when one paragraph or one claim is by itself larger than the whole
    chunk budget -- rare, but a 4,000-word paragraph must not be allowed to blow the
    context window. Overlap applies HERE and only here: this is the one place where a
    cut can land mid-argument, so the pieces are given a shared tail to keep a sentence
    straddling the boundary retrievable from either side. Paragraph-aligned merges need
    no overlap because they never cut mid-idea.
    """
    sentences = split_sentences(text) or [text]
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        # A single sentence longer than the budget: hard-cut it. Nothing smarter is
        # available and dropping it would silently lose source text.
        if len(sentence) > budget_chars:
            if current:
                pieces.append(" ".join(current))
                current, current_len = [], 0
            for start in range(0, len(sentence), budget_chars):
                pieces.append(sentence[start : start + budget_chars])
            continue
        if current_len + len(sentence) + 1 > budget_chars and current:
            pieces.append(" ".join(current))
            tail = current[-1] if overlap_chars and len(current[-1]) <= overlap_chars else ""
            current = [tail] if tail else []
            current_len = len(tail)
        current.append(sentence)
        current_len += len(sentence) + 1
    if current:
        pieces.append(" ".join(current))
    return [p for p in (piece.strip() for piece in pieces) if p]


def chunk_source_document(
    doc: SourceDocument,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    strategy: str | None = None,
) -> list[RawChunk]:
    """Cut a judgment into retrieval units.

    ``strategy="grouped"`` merges paragraphs greedily up to the retrieval target;
    ``strategy="paragraph"`` emits one unit per paragraph and merges only stubs
    forward. In both, a new chunk is forced whenever the heading path changes: a chunk
    spanning two sections would carry a heading path true of only half of it.

    ``CHUNK_TARGET_TOKENS`` caps a unit in both modes; what the strategy changes is
    when a unit is CLOSED. That number came from F9 -- a judgment is ~21k tokens
    against a 16k context, so chunking is mandatory -- which answers "what fits the
    model", not "what should a one-sentence claim be compared against". Grouping to the
    ceiling answered only the first question, and gave a median unit of ~6 paragraphs.

    Falls back to splitting the raw ``doc.text`` when the document carries no parsed
    paragraphs, so a source that was fetched but not marked up is still assessable
    rather than silently scoring zero.
    """
    strategy = strategy or settings.CHUNK_STRATEGY
    per_paragraph = strategy == "paragraph"
    target_tokens = target_tokens or settings.CHUNK_TARGET_TOKENS
    overlap_tokens = settings.CHUNK_OVERLAP_TOKENS if overlap_tokens is None else overlap_tokens
    overlap = _budget_chars(overlap_tokens) if overlap_tokens else 0

    budget = _budget_chars(target_tokens)
    # The only difference between the modes: when the accumulator is closed. Grouping
    # fills it to the budget; paragraph mode closes it as soon as it holds enough text
    # to be worth embedding, so a stub rides along with the paragraph after it.
    min_chars = settings.CHUNK_MIN_CHARS if per_paragraph else budget

    paragraphs = [p for p in doc.paragraphs if p.kind in _CONTENT_KINDS and p.text.strip()]
    if not paragraphs:
        return _chunk_unstructured_text(doc.text, budget, overlap)

    chunks: list[RawChunk] = []
    acc = _Accumulator()
    current_path: tuple[str, ...] = paragraphs[0].heading_path

    def flush() -> None:
        if not acc.paragraphs:
            return
        numbers = [p.paragraph_number for p in acc.paragraphs if p.paragraph_number is not None]
        text = "\n\n".join(p.text.strip() for p in acc.paragraphs)
        for piece in _fit(text, budget, overlap):
            chunks.append(
                RawChunk(
                    ordinal=len(chunks),
                    kind=ChunkKind.BODY,
                    text=piece,
                    heading_path=current_path,
                    paragraph_from=min(numbers) if numbers else None,
                    paragraph_to=max(numbers) if numbers else None,
                    strategy=strategy,
                )
            )
        acc.clear()

    for para in paragraphs:
        text = para.text.strip()
        if para.heading_path != current_path:
            flush()
            current_path = para.heading_path
        if acc.paragraphs and acc.chars + len(text) + 2 > budget:
            flush()
        acc.paragraphs.append(para)
        acc.chars += len(text) + 2
        # Paragraph mode closes the unit as soon as it carries enough text to be worth
        # embedding. A stub therefore rides along with the paragraph after it rather
        # than becoming a chunk of its own.
        if per_paragraph and acc.chars >= min_chars:
            flush()
    flush()

    _assert_within_budget(chunks, budget)
    return chunks


def _chunk_unstructured_text(text: str, budget: int, overlap: int) -> list[RawChunk]:
    chunks = [
        RawChunk(ordinal=i, kind=ChunkKind.BODY, text=piece, strategy="unstructured")
        for i, piece in enumerate(_fit(text, budget, overlap))
    ]
    _assert_within_budget(chunks, budget)
    return chunks


def _fit(text: str, budget: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= budget:
        return [text]
    return _split_oversized(text, budget, overlap)


def _assert_within_budget(chunks: list[RawChunk], budget: int) -> None:
    """Hard invariant: no chunk may exceed the context budget.

    This is an assertion rather than a log line because a chunk over the limit is
    silently truncated by the provider -- there is no error, just a similarity score
    computed against text the model never saw.
    """
    for chunk in chunks:
        assert len(chunk.text) <= budget, (
            f"chunk {chunk.ordinal} is {len(chunk.text)} chars, over the {budget}-char "
            f"budget ({estimate_tokens(chunk.text)} > {budget // CHARS_PER_TOKEN} tokens)"
        )


def window_claims(
    text: str,
    *,
    window: int = 2,
    stride: int = 1,
    target_tokens: int | None = None,
) -> list[RawChunk]:
    """Deterministic sentence windows: the fallback claim splitter.

    Two sentences with a one-sentence stride, so a claim whose subject is in one
    sentence and whose authority is in the next is never split away from its support.

    NO LLM ON THIS PATH, deliberately. It runs when the summariser is unavailable, in
    mock mode, or when ``split_claims`` returns something unparseable -- all situations
    where adding a model call would add latency to a path that is already degraded.
    Spans are exact here because the windows are cut from the output itself.
    """
    target_tokens = target_tokens or settings.CHUNK_TARGET_TOKENS
    budget = _budget_chars(target_tokens)
    sentences = split_sentences(text)
    if not sentences:
        stripped = text.strip()
        if not stripped:
            return []
        sentences = [stripped]

    spans = _locate_sequential(text, sentences)
    chunks: list[RawChunk] = []
    for start in range(0, len(sentences), stride):
        group = sentences[start : start + window]
        if not group:
            break
        group_spans = [s for s in spans[start : start + window] if s is not None]
        piece = " ".join(group)
        span = Span(start=group_spans[0].start, end=group_spans[-1].end) if group_spans else None
        for sub in _fit(piece, budget, 0):
            chunks.append(
                RawChunk(
                    ordinal=len(chunks),
                    kind=ChunkKind.WINDOW,
                    text=sub,
                    span=span if sub == piece else None,
                    strategy="window",
                )
            )
        if start + window >= len(sentences):
            break
    return chunks


def _locate_sequential(haystack: str, needles: list[str]) -> list[Span | None]:
    """Find each needle in order, so repeated sentences map to distinct offsets."""
    spans: list[Span | None] = []
    cursor = 0
    for needle in needles:
        idx = haystack.find(needle, cursor)
        if idx == -1:
            spans.append(None)
            continue
        spans.append(Span(start=idx, end=idx + len(needle)))
        cursor = idx + len(needle)
    return spans


def locate_claim(haystack: str, claim: str, *, min_score: float = 70.0) -> Span | None:
    """Map a model-produced claim back to offsets in the AI output.

    ``split_claims`` returns atomic claims that are usually, but not always, verbatim
    slices -- a model will happily resolve a pronoun or drop a parenthetical. An exact
    ``find`` therefore misses claims that are genuinely present, so we fall back to
    fuzzy alignment. Returning ``None`` is a real answer: L3 attributes claims to
    citations by position, and a claim we cannot locate must not be attributed to a
    citation by guesswork.
    """
    claim = claim.strip()
    if not claim or not haystack:
        return None
    idx = haystack.find(claim)
    if idx != -1:
        return Span(start=idx, end=idx + len(claim))
    from rapidfuzz import fuzz

    alignment = fuzz.partial_ratio_alignment(claim, haystack)
    if alignment is None or alignment.score < min_score:
        return None
    return Span(start=alignment.dest_start, end=alignment.dest_end)


async def chunk_output_claims(
    text: str,
    *,
    summariser: Summariser | None = None,
    target_tokens: int | None = None,
) -> list[RawChunk]:
    """Split an AI output into claim-sized units.

    Primary path is ``Summariser.split_claims``; the deterministic sentence windows are
    the fallback for a missing summariser, an empty or unparseable response, or any
    provider error. The fallback is not a degraded mode we tolerate -- it is what keeps
    L3 and L4 running at full speed when the LLM tier is slow, down, or absent.
    """
    if not text.strip():
        return []
    claims: list[str] = []
    if summariser is not None:
        try:
            raw = await summariser.split_claims(text)
            claims = [c.strip() for c in raw or [] if c and c.strip()]
        except Exception:  # noqa: BLE001 - any provider failure falls back, never fails the run
            claims = []
    if not claims:
        return window_claims(text, target_tokens=target_tokens)

    target_tokens = target_tokens or settings.CHUNK_TARGET_TOKENS
    budget = _budget_chars(target_tokens)
    chunks: list[RawChunk] = []
    for claim in claims:
        span = locate_claim(text, claim)
        for sub in _fit(claim, budget, 0):
            chunks.append(
                RawChunk(
                    ordinal=len(chunks),
                    kind=ChunkKind.CLAIM,
                    text=sub,
                    span=span if sub == claim else None,
                    strategy="claims",
                )
            )
    return chunks or window_claims(text, target_tokens=target_tokens)
