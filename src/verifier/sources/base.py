"""Source adapters: how a citation becomes a document, per jurisdiction and court."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from verifier.contracts.citations import ExtractedCitation, Resolution
from verifier.contracts.documents import SourceDocument
from verifier.providers.base import SearchHit


@runtime_checkable
class SourceAdapter(Protocol):
    """One per corpus. eLitigation is the only real implementation for now; the
    protocol exists so a second jurisdiction is an addition, not a refactor."""

    name: str
    domain: str

    def build_url(self, citation: ExtractedCitation) -> str | None:
        """Deterministic URL for a neutral citation, or None if not derivable.

        For eLitigation: [2007] SGCA 37 -> /gd/s/2007_SGCA_37. Parenthesised court
        suffixes are stripped, not encoded: SGHC(A) -> SGHCA.
        """
        ...

    async def search(self, phrase: str, *, limit: int = 10) -> list[SearchHit]:
        """Full-text search by case name.

        Note: this index is full-text over judgment BODIES, so searching a report
        citation returns cases that CITE it, not the case itself. Report-only
        citations are therefore unresolvable and must never be failed.
        """
        ...

    async def resolve(self, citation: ExtractedCitation) -> Resolution: ...

    def parse(self, html: str, url: str) -> SourceDocument:
        """HTML -> structured document, including soft-404 detection.

        A fabricated citation returns HTTP 200 with a small body and an empty
        <title>, so the status code carries no signal and this parser carries it all.
        """
        ...
