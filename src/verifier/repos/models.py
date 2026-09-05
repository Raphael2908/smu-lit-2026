"""SQLAlchemy 2.0 ORM models mapping the tables created by ``alembic/0001_initial``.

These declare no schema of their own: the migration is the source of truth and is
frozen. If a column is not here, it is not in the migration either -- and the fix is a
new migration authored by whoever owns the schema, never a silent divergence.

Where ``RunState`` carries a field the frozen schema has no column for (``errors``,
``timings.extract_ms``, ``is_final``, ``LayerResult.cache_misses``, ``Finding.id``),
the repo layer stores it inside an existing JSON column under an underscore-prefixed
key and unpacks it tolerantly on read. That is deliberate and documented at each site:
adding a migration mid-fan-out deadlocks the revision chain, which is the single most
expensive avoidable failure in a parallel build.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Mirrors ``EMBED_DIM`` in alembic/versions/0001_initial.py. Changing
#: ``settings.EMBEDDINGS_DIM`` alone does NOT change the column -- that needs a migration.
EMBED_DIM = 1024


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_domain: Mapped[str] = mapped_column(Text, nullable=False)
    fetch_strategy: Mapped[str] = mapped_column(Text, nullable=False, default="http")
    fetched_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    #: A fabricated citation returns HTTP 200 with a small body and an empty <title>
    #: (F3), so the status code carries no signal and this flag carries it all.
    is_soft_404: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exists: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    neutral_citation: Mapped[str | None] = mapped_column(Text)
    case_name: Mapped[str | None] = mapped_column(Text)
    court: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer)
    decision_date: Mapped[dt.date | None] = mapped_column(Date)
    coram: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text_sha256: Mapped[str | None] = mapped_column(Text)
    char_len: Mapped[int | None] = mapped_column(Integer, default=0)
    parallel_citations: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=list)
    cited_authorities: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=list)


class DocumentParagraph(Base):
    __tablename__ = "document_paragraphs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The '[115]' a pinpoint citation refers to. Verifying a quote against one
    #: paragraph instead of ~84k chars is a large precision win for partial_ratio.
    para_no: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="body")
    heading_path: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=list)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class CitationResolution(Base):
    __tablename__ = "citation_resolutions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    citation_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    citation_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="SET NULL")
    )
    #: JSON envelope, not a bare list -- see repos/resolutions.py for why.
    candidates: Mapped[Any | None] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, default=0.0)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class TextEmbedding(Base):
    __tablename__ = "text_embeddings"
    __table_args__ = (UniqueConstraint("model", "input_sha256", name="uq_embedding_model_input"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    #: sha256 of the FULL embed input (summary + heading path + text), so changing the
    #: summary prompt correctly invalidates the cache.
    input_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=EMBED_DIM)
    embedding: Mapped[Any] = mapped_column(Vector(EMBED_DIM), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE")
    )
    created_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentSummaryRow(Base):
    __tablename__ = "document_summaries"
    __table_args__ = (
        UniqueConstraint("document_id", "model", "prompt_version", name="uq_summary"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE")
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False, default="v1")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int | None] = mapped_column(Integer, default=0)


class ChunkRow(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documents.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="body")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str | None] = mapped_column(Text)
    embed_input_sha256: Mapped[str | None] = mapped_column(Text)
    para_from: Mapped[int | None] = mapped_column(Integer)
    para_to: Mapped[int | None] = mapped_column(Integer)


class ListEntry(Base):
    """Source trust list rows.

    The MODEL lives here because models.py owns the ORM mapping of the frozen schema;
    the ``ListRepo`` IMPLEMENTATION is Stream B's (``repos/lists.py``). ``extend_existing``
    keeps a second declaration of the same table on this Base from exploding at import.
    """

    __tablename__ = "list_entries"
    __table_args__ = (
        CheckConstraint("list_type IN ('white','gray','black')", name="ck_list_type"),
        {"extend_existing": True},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    list_type: Mapped[str] = mapped_column(Text, nullable=False)
    match_type: Mapped[str] = mapped_column(Text, nullable=False, default="domain")
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    ai_output: Mapped[str] = mapped_column(Text, nullable=False)
    ai_output_sha256: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    #: JSON envelope: the request's ``client`` dict plus the RunState fields the frozen
    #: schema has no column for. See repos/runs.py::_pack_envelope.
    client: Mapped[Any | None] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    verdict: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    verdict_stage: Mapped[str | None] = mapped_column(Text)
    short_circuited: Mapped[bool | None] = mapped_column(Boolean, default=False)
    short_circuit_reason: Mapped[str | None] = mapped_column(Text)
    deterministic_ms: Mapped[int | None] = mapped_column(Integer, default=0)
    judge_ms: Mapped[int | None] = mapped_column(Integer, default=0)
    total_ms: Mapped[int | None] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Any | None] = mapped_column(Numeric(10, 6), default=0)
    cache_hits: Mapped[int | None] = mapped_column(Integer, default=0)
    cache_misses: Mapped[int | None] = mapped_column(Integer, default=0)
    seq: Mapped[int | None] = mapped_column(Integer, default=0)


class RunCitation(Base):
    __tablename__ = "run_citations"
    __table_args__ = (PrimaryKeyConstraint("run_id", "ordinal"),)

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    citation_type: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_key: Mapped[str | None] = mapped_column(Text)
    span_start: Mapped[int | None] = mapped_column(Integer)
    span_end: Mapped[int | None] = mapped_column(Integer)
    resolution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("citation_resolutions.id", ondelete="SET NULL")
    )


class RunSource(Base):
    __tablename__ = "run_sources"
    __table_args__ = (PrimaryKeyConstraint("run_id", "ordinal"),)

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_ref: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str | None] = mapped_column(Text)
    list_type: Mapped[str | None] = mapped_column(Text)
    list_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("list_entries.id", ondelete="SET NULL")
    )


class RunQuote(Base):
    __tablename__ = "run_quotes"
    __table_args__ = (PrimaryKeyConstraint("run_id", "ordinal"),)

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    delimiter: Mapped[str | None] = mapped_column(Text)
    attributed_citation_ordinal: Mapped[int | None] = mapped_column(Integer)
    attribution_method: Mapped[str | None] = mapped_column(Text)
    pinpoint_para: Mapped[int | None] = mapped_column(Integer)


class LayerResultRow(Base):
    __tablename__ = "layer_results"
    __table_args__ = (PrimaryKeyConstraint("run_id", "layer"),)

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id", ondelete="CASCADE"))
    layer: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int | None] = mapped_column(Integer, default=0)
    detail: Mapped[Any | None] = mapped_column(JSON, default=dict)


class FindingRow(Base):
    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE")
    )
    layer: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    citation_ordinal: Mapped[int | None] = mapped_column(Integer)
    quote_ordinal: Mapped[int | None] = mapped_column(Integer)
    span_start: Mapped[int | None] = mapped_column(Integer)
    span_end: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[Any | None] = mapped_column(JSON, default=dict)
    #: 'deterministic' vs 'llm'. The UI renders these differently; that separation is
    #: how a user sees which findings are machine-checkable ground truth.
    source: Mapped[str] = mapped_column(Text, nullable=False, default="deterministic")


class JudgeCall(Base):
    """Full prompt/response provenance -- the auditor's own audit trail."""

    __tablename__ = "judge_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(Text, default="v1")
    request: Mapped[Any | None] = mapped_column(JSON, default=dict)
    response_raw: Mapped[str | None] = mapped_column(Text)
    parsed: Mapped[Any | None] = mapped_column(JSON)
    parse_path: Mapped[str | None] = mapped_column(Text)
    retries: Mapped[int | None] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Any | None] = mapped_column(Numeric(10, 6), default=0)
    created_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
