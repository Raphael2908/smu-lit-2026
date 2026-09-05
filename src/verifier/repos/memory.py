"""In-memory repository implementations.

These satisfy the same protocols as the Postgres ones, so the entire backend is
exercisable offline with no database. Tests run against these; production swaps them
out at the factory boundary without any layer noticing.
"""

from __future__ import annotations

import fnmatch
import hashlib
import random
from uuid import uuid4

from verifier.contracts.citations import Resolution
from verifier.contracts.documents import DocumentSummary, SourceDocument
from verifier.contracts.enums import ListType, MatchType
from verifier.contracts.runs import RunState


class InMemoryDocumentRepo:
    def __init__(self) -> None:
        self._docs: dict[str, SourceDocument] = {}
        self._summaries: dict[tuple[str, str, str], DocumentSummary] = {}

    async def get_by_url(self, url: str) -> SourceDocument | None:
        return self._docs.get(url)

    async def upsert(self, doc: SourceDocument) -> SourceDocument:
        stored = doc if doc.id else doc.model_copy(update={"id": str(uuid4())})
        self._docs[stored.source_url] = stored
        return stored

    async def get_summary(
        self, document_id: str, model: str, prompt_version: str
    ) -> DocumentSummary | None:
        return self._summaries.get((document_id, model, prompt_version))

    async def put_summary(self, summary: DocumentSummary) -> None:
        key = (summary.document_id, summary.model, summary.prompt_version)
        self._summaries[key] = summary


class InMemoryResolutionRepo:
    def __init__(self) -> None:
        self._items: dict[str, Resolution] = {}

    async def get(self, citation_key: str) -> Resolution | None:
        return self._items.get(citation_key)

    async def put(self, resolution: Resolution) -> None:
        self._items[resolution.citation_key] = resolution


class InMemoryEmbeddingRepo:
    def __init__(self) -> None:
        self._vectors: dict[tuple[str, str], list[float]] = {}
        self._by_document: dict[str, list[str]] = {}

    async def get_many(self, model: str, input_hashes: list[str]) -> dict[str, list[float]]:
        return {h: self._vectors[(model, h)] for h in input_hashes if (model, h) in self._vectors}

    async def put_many(
        self, model: str, vectors: dict[str, list[float]], document_id: str | None = None
    ) -> None:
        for h, vec in vectors.items():
            self._vectors[(model, h)] = vec
            if document_id:
                self._by_document.setdefault(document_id, []).append(h)

    async def sample_background(
        self, model: str, limit: int, exclude_document_id: str | None = None
    ) -> list[list[float]]:
        """Chunks from OTHER documents, for L3's contrastive margin.

        Deterministically seeded so a test's margin does not wobble between runs.
        """
        excluded = set(self._by_document.get(exclude_document_id or "", []))
        pool = [v for (m, h), v in self._vectors.items() if m == model and h not in excluded]
        if len(pool) <= limit:
            return pool
        return random.Random(0).sample(pool, limit)


class InMemoryListRepo:
    """Source trust lists. Longest pattern wins, so a specific subdomain rule beats a
    broad one; black is checked before gray so an explicit block is never softened."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    async def match(self, domain: str) -> tuple[ListType, str] | None:
        domain = (domain or "").lower().lstrip(".")
        if not domain:
            return None
        best: tuple[int, ListType, str] | None = None
        for entry in self._entries.values():
            if not entry["active"]:
                continue
            pattern = entry["pattern"].lower()
            matched = (
                fnmatch.fnmatch(domain, pattern)
                if entry["match_type"] == MatchType.URL_PATTERN
                else domain == pattern or domain.endswith("." + pattern)
            )
            if matched and (best is None or len(pattern) > best[0]):
                best = (len(pattern), ListType(entry["list_type"]), entry["reason"])
        return (best[1], best[2]) if best else None

    async def add(
        self, list_type: ListType, match_type: MatchType, pattern: str, reason: str = ""
    ) -> str:
        entry_id = str(uuid4())
        self._entries[entry_id] = {
            "id": entry_id,
            "list_type": list_type,
            "match_type": match_type,
            "pattern": pattern,
            "reason": reason,
            "active": True,
        }
        return entry_id

    async def remove(self, entry_id: str) -> bool:
        return self._entries.pop(entry_id, None) is not None

    async def all(self) -> list[dict]:
        return list(self._entries.values())


class InMemoryRunRepo:
    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._by_key: dict[str, str] = {}

    async def create(self, state: RunState) -> RunState:
        self._runs[state.run_id] = state
        return state

    async def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    async def save(self, state: RunState) -> RunState:
        self._runs[state.run_id] = state
        return state

    async def get_by_idempotency_key(self, key: str) -> RunState | None:
        run_id = self._by_key.get(key)
        return self._runs.get(run_id) if run_id else None

    async def register_key(self, key: str, run_id: str) -> None:
        self._by_key[key] = run_id


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
