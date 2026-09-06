"""Provider protocols. Orchestration depends only on these, so mock<->real is a drop-in.

Rule: when adding a vendor capability, change the provider behind the interface --
never the orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from verifier.contracts.documents import SourceDocument


@dataclass(frozen=True)
class FetchResult:
    """Raw fetch outcome, before any judgement about whether the citation is real."""

    url: str
    status_code: int
    html: str
    elapsed_ms: int
    from_cache: bool = False
    authenticated: bool = False


@dataclass(frozen=True)
class SearchHit:
    neutral_citation: str
    url: str
    case_name: str
    rank: int


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    tokens: int = 0
    cache_hits: int = 0
    cache_misses: int = 0


@dataclass(frozen=True)
class JudgeRubric:
    """What the judge scored.

    The active prompt scores two BINARY dimensions -- substantive correctness and
    material completeness -- assessed independently, because an answer can be entirely
    true and still materially misleading by omission, or cover every issue and get one
    wrong. Those are ``correctness`` and ``material_completeness``.

    The four 0-4 fields are the earlier rubric shape, retained so a differently
    prompted judge still has somewhere to report. A given judge populates one set or
    the other; ``None`` means "this judge did not score that dimension", which is not
    the same as scoring it zero.
    """

    factual_faithfulness: int | None = None
    contextual_accuracy: int | None = None
    citation_integrity: int | None = None
    responsiveness: int | None = None
    correctness: int | None = None
    material_completeness: int | None = None


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    rubric: JudgeRubric | None
    reasons: list[str] = field(default_factory=list)
    raw_response: str = ""
    parse_path: str = "strict"
    retries: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    model: str = ""
    provider: str = ""


@dataclass(frozen=True)
class CitationCandidate:
    """One citation the extractor believes the output offered.

    ``raw_text`` must be COPIED FROM THE OUTPUT, character for character. Everything
    downstream depends on it: the text is located in the answer by substring search, and
    the span that search returns is the citation's span. A model that normalises a curly
    quote, expands an abbreviation or silently fixes a typo produces a string that cannot
    be found, and the candidate is dropped rather than trusted.

    ``url`` is the link the OUTPUT gave, or None. It is not somewhere for the model to
    supply a URL it believes to be correct: an invented link would be fetched.
    """

    raw_text: str
    url: str | None = None


@dataclass(frozen=True)
class CitationExtraction:
    """What one extraction call produced.

    ``degraded`` carries WHY the extractor contributed nothing -- a timeout, a missing
    key, unparseable output. It is not decoration. L0's FAIL means "this output cited
    nothing", and without this field an extractor that never ran is indistinguishable
    from an answer that cited nothing at all.
    """

    citations: tuple[CitationCandidate, ...] = ()
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    parse_path: str = "strict"
    degraded: str | None = None


@runtime_checkable
class Fetcher(Protocol):
    """HTTP or browser. Layers never know which was used."""

    strategy: str

    async def fetch(self, url: str) -> FetchResult: ...
    async def healthy(self) -> bool: ...


@runtime_checkable
class Embedder(Protocol):
    model: str
    dim: int

    async def embed(self, texts: list[str], *, input_type: str | None = None) -> EmbeddingResult:
        """input_type is 'query' or 'document'.

        This asymmetry is load-bearing, not optional: a short interrogative and a long
        declarative passage only land in the same retrieval space when the model is
        told which is which. Without it a perfect answer scores low purely from length
        and register mismatch, and every threshold derived from it is meaningless.
        """
        ...


@runtime_checkable
class Summariser(Protocol):
    model: str

    async def summarise_document(self, doc: SourceDocument) -> str: ...
    async def split_claims(self, text: str) -> list[str]: ...


@runtime_checkable
class Judge(Protocol):
    model: str
    provider: str

    async def judge(self, *, system_prompt: str, payload: dict) -> JudgeResult: ...


@runtime_checkable
class CitationExtractor(Protocol):
    """Finds the citations an answer offers. It does not judge whether they are good.

    Whether a citation exists is L1's question, answered deterministically against the
    real corpus. This interface exists only to widen what reaches it, because a regex
    recognises the citation forms someone enumerated and an answer may use one nobody
    did (docs/03-findings.md F13).

    An implementation MUST NOT raise to signal failure. Return a ``CitationExtraction``
    with ``degraded`` set instead: an extractor that is down must never be reported to a
    lawyer as an answer that cited nothing.
    """

    model: str
    provider: str

    async def extract_citations(self, ai_output: str) -> CitationExtraction: ...
