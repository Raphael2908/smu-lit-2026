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
    """Scored 0-4 each, with written justification. factual_faithfulness is weighted
    highest: it is the dimension embeddings provably cannot assess."""

    factual_faithfulness: int
    contextual_accuracy: int
    citation_integrity: int
    responsiveness: int


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
