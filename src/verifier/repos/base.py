"""Repository protocols. Handlers and layers never touch the DB driver directly."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from verifier.contracts.citations import Resolution
from verifier.contracts.documents import DocumentSummary, SourceDocument
from verifier.contracts.enums import ListType, MatchType
from verifier.contracts.runs import RunState


@runtime_checkable
class DocumentRepo(Protocol):
    async def get_by_url(self, url: str) -> SourceDocument | None: ...
    async def upsert(self, doc: SourceDocument) -> SourceDocument: ...
    async def get_summary(
        self, document_id: str, model: str, prompt_version: str
    ) -> DocumentSummary | None: ...
    async def put_summary(self, summary: DocumentSummary) -> None: ...


@runtime_checkable
class ResolutionRepo(Protocol):
    async def get(self, citation_key: str) -> Resolution | None: ...
    async def put(self, resolution: Resolution) -> None: ...


@runtime_checkable
class EmbeddingRepo(Protocol):
    """Content-hash keyed embedding cache. This is the scalability story: the second
    query touching a given case pays nothing."""

    async def get_many(self, model: str, input_hashes: list[str]) -> dict[str, list[float]]: ...
    async def put_many(self, model: str, vectors: dict[str, list[float]]) -> None: ...
    async def sample_background(
        self, model: str, limit: int, exclude_document_id: str | None = None
    ) -> list[list[float]]:
        """Chunks from OTHER cached judgments, for L3's contrastive margin.

        Must span several areas of law. If it is accidentally seeded with judgments on
        the query's own topic, margins collapse and everything looks ungrounded.
        """
        ...


@runtime_checkable
class ListRepo(Protocol):
    async def match(self, domain: str) -> tuple[ListType, str] | None:
        """Returns (list_type, reason) for the most specific matching entry, or None."""
        ...

    async def add(
        self, list_type: ListType, match_type: MatchType, pattern: str, reason: str
    ) -> str: ...
    async def remove(self, entry_id: str) -> bool: ...
    async def all(self) -> list[dict]: ...


@runtime_checkable
class RunRepo(Protocol):
    async def create(self, state: RunState) -> RunState: ...
    async def get(self, run_id: str) -> RunState | None: ...
    async def save(self, state: RunState) -> RunState: ...
    async def get_by_idempotency_key(self, key: str) -> RunState | None: ...
