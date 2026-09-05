"""Fetched source documents and their chunks. Frozen contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from verifier.contracts.enums import ChunkKind, FetchStrategy


class Paragraph(BaseModel):
    """A numbered paragraph of a judgment.

    eLitigation marks these up cleanly (Judg-1, Judg-Quote-1, Judg-Heading-*), which
    gives us paragraph chunking, a heading hierarchy and pinpoint lookup for free (F5).
    """

    model_config = ConfigDict(frozen=True)

    ordinal: int
    paragraph_number: int | None = None  # the '[115]' a pinpoint cite refers to
    kind: ChunkKind
    heading_path: tuple[str, ...] = ()
    text: str


class SourceDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str | None = None
    source_url: str
    domain: str
    fetch_strategy: FetchStrategy

    #: False for a soft-404. eLitigation returns HTTP 200 for fabricated citations
    #: (F3), so the status code is useless and this flag carries the real signal.
    exists: bool
    is_soft_404: bool = False
    http_status: int | None = None

    neutral_citation: str | None = None  # from <title>; confirms we resolved the right case
    case_name: str | None = None
    court: str | None = None
    year: int | None = None
    coram: str | None = None

    text: str = ""
    text_sha256: str = ""
    paragraphs: tuple[Paragraph, ...] = ()
    #: From <nobr> tags. Lets a report-only citation resolve later via the cache.
    parallel_citations: tuple[str, ...] = ()
    #: The judgment's own citation graph -- free, and the hook for a future bias layer.
    cited_authorities: tuple[str, ...] = ()

    def paragraph(self, number: int) -> Paragraph | None:
        return next((p for p in self.paragraphs if p.paragraph_number == number), None)


class Chunk(BaseModel):
    """An embeddable unit. ``embed_input`` is what was actually sent to the model
    (summary + heading path + text), and its hash is the cache key -- so changing the
    summary prompt correctly invalidates the cache."""

    model_config = ConfigDict(frozen=True)

    ordinal: int
    kind: ChunkKind
    text: str
    embed_input: str
    embed_input_sha256: str
    document_id: str | None = None
    paragraph_from: int | None = None
    paragraph_to: int | None = None


class DocumentSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: str
    model: str
    prompt_version: str
    summary: str
    tokens: int = Field(default=0, ge=0)
