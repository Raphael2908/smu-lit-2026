"""Initial schema: caches, source trust lists, runs and findings.

The ENTIRE schema lives in this one migration on purpose. Parallel workstreams that
each author their own migration deadlock on the revision chain, and that is the most
expensive avoidable failure in a fan-out build.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBED_DIM = 1024  # voyage-law-2


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- source cache -------------------------------------------------------
    # Fetching is the most expensive thing we do (worst case an authenticated
    # browser session), so it happens once per case, ever.
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_url", sa.Text, nullable=False, unique=True),
        sa.Column("source_domain", sa.Text, nullable=False),
        sa.Column("fetch_strategy", sa.Text, nullable=False, server_default="http"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("http_status", sa.Integer),
        # A fabricated citation returns HTTP 200 with a small body and an empty
        # <title>, so the status code carries no signal and this flag carries it all.
        sa.Column("is_soft_404", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("exists", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("neutral_citation", sa.Text),
        sa.Column("case_name", sa.Text),
        sa.Column("court", sa.Text),
        sa.Column("year", sa.Integer),
        sa.Column("decision_date", sa.Date),
        sa.Column("coram", sa.Text),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("text_sha256", sa.Text),
        sa.Column("char_len", sa.Integer, server_default="0"),
        # From <nobr> tags: lets a report-only citation resolve later via the cache.
        sa.Column("parallel_citations", sa.ARRAY(sa.Text), server_default="{}"),
        # The judgment's own citation graph. Free from the markup, and the hook for a
        # future bias layer.
        sa.Column("cited_authorities", sa.ARRAY(sa.Text), server_default="{}"),
    )
    op.create_index("ix_documents_neutral_citation", "documents", ["neutral_citation"])
    op.create_index("ix_documents_text_sha256", "documents", ["text_sha256"])

    op.create_table(
        "document_paragraphs",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id", sa.Uuid, sa.ForeignKey("documents.id", ondelete="CASCADE")),
        sa.Column("ordinal", sa.Integer, nullable=False),
        # The '[115]' that a pinpoint citation refers to. Verifying a quote against one
        # paragraph instead of ~84k chars is a large precision win for partial_ratio.
        sa.Column("para_no", sa.Integer),
        sa.Column("kind", sa.Text, nullable=False, server_default="body"),
        sa.Column("heading_path", sa.ARRAY(sa.Text), server_default="{}"),
        sa.Column("text", sa.Text, nullable=False),
    )
    op.create_index("ix_paragraphs_doc_para", "document_paragraphs", ["document_id", "para_no"])

    op.create_table(
        "citation_resolutions",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("citation_key", sa.Text, nullable=False, unique=True),
        sa.Column("citation_type", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("method", sa.Text, nullable=False, server_default="none"),
        sa.Column("document_id", sa.Uuid, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("candidates", sa.JSON, server_default="[]"),
        sa.Column("confidence", sa.Float, server_default="0"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )

    # --- content-hash keyed caches -----------------------------------------
    op.create_table(
        "text_embeddings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("model", sa.Text, nullable=False),
        # sha256 of the FULL embed input (summary + heading path + text), so changing
        # the summary prompt correctly invalidates the cache.
        sa.Column("input_sha256", sa.Text, nullable=False),
        sa.Column("dim", sa.Integer, nullable=False, server_default=str(EMBED_DIM)),
        sa.Column("embedding", Vector(EMBED_DIM), nullable=False),
        sa.Column("document_id", sa.Uuid, sa.ForeignKey("documents.id", ondelete="CASCADE")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("model", "input_sha256", name="uq_embedding_model_input"),
    )
    # Comparisons are scoped to one document's chunks (small N), so ANN indexing is not
    # needed for correctness. HNSW is here for BACKGROUND sampling and future scale.
    op.execute(
        "CREATE INDEX ix_text_embeddings_hnsw ON text_embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "document_summaries",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id", sa.Uuid, sa.ForeignKey("documents.id", ondelete="CASCADE")),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False, server_default="v1"),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("tokens", sa.Integer, server_default="0"),
        sa.UniqueConstraint("document_id", "model", "prompt_version", name="uq_summary"),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id", sa.Uuid, sa.ForeignKey("documents.id", ondelete="CASCADE")),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False, server_default="body"),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("text_sha256", sa.Text),
        sa.Column("embed_input_sha256", sa.Text),
        sa.Column("para_from", sa.Integer),
        sa.Column("para_to", sa.Integer),
    )
    op.create_index("ix_chunks_document", "chunks", ["document_id"])

    # --- source trust lists -------------------------------------------------
    # L2 asks "is this source trustworthy?", which is a different question from L1's
    # "does this citation exist?". Both must pass, so whitelisting a source can never
    # launder a fabricated citation.
    op.create_table(
        "list_entries",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("list_type", sa.Text, nullable=False),
        sa.Column("match_type", sa.Text, nullable=False, server_default="domain"),
        sa.Column("pattern", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, server_default=""),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("list_type IN ('white','gray','black')", name="ck_list_type"),
    )
    op.create_index("ix_list_lookup", "list_entries", ["list_type", "match_type", "active"])

    # --- runs ---------------------------------------------------------------
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("ai_output", sa.Text, nullable=False),
        sa.Column("ai_output_sha256", sa.Text),
        sa.Column("idempotency_key", sa.Text, unique=True),
        sa.Column("client", sa.JSON, server_default="{}"),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("verdict", sa.Text, nullable=False, server_default="pending"),
        sa.Column("verdict_stage", sa.Text),
        # True when a deterministic failure meant the judge was never consulted. The
        # panel shows this explicitly: it is the invariant made legible.
        sa.Column("short_circuited", sa.Boolean, server_default=sa.false()),
        sa.Column("short_circuit_reason", sa.Text),
        sa.Column("deterministic_ms", sa.Integer, server_default="0"),
        sa.Column("judge_ms", sa.Integer, server_default="0"),
        sa.Column("total_ms", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), server_default="0"),
        sa.Column("cache_hits", sa.Integer, server_default="0"),
        sa.Column("cache_misses", sa.Integer, server_default="0"),
        sa.Column("seq", sa.Integer, server_default="0"),
    )

    op.create_table(
        "run_citations",
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("citation_type", sa.Text, nullable=False),
        sa.Column("normalized_key", sa.Text),
        sa.Column("span_start", sa.Integer),
        sa.Column("span_end", sa.Integer),
        sa.Column("resolution_id", sa.Uuid,
                  sa.ForeignKey("citation_resolutions.id", ondelete="SET NULL")),
        sa.PrimaryKeyConstraint("run_id", "ordinal"),
    )

    op.create_table(
        "run_sources",
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("raw_ref", sa.Text, nullable=False),
        sa.Column("domain", sa.Text),
        sa.Column("list_type", sa.Text),
        sa.Column("list_entry_id", sa.Uuid,
                  sa.ForeignKey("list_entries.id", ondelete="SET NULL")),
        sa.PrimaryKeyConstraint("run_id", "ordinal"),
    )

    op.create_table(
        "run_quotes",
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("quote_text", sa.Text, nullable=False),
        sa.Column("delimiter", sa.Text),
        sa.Column("attributed_citation_ordinal", sa.Integer),
        sa.Column("attribution_method", sa.Text),
        sa.Column("pinpoint_para", sa.Integer),
        sa.PrimaryKeyConstraint("run_id", "ordinal"),
    )

    op.create_table(
        "layer_results",
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("layer", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("score", sa.Float),
        sa.Column("duration_ms", sa.Integer, server_default="0"),
        sa.Column("cache_hits", sa.Integer, server_default="0"),
        sa.Column("detail", sa.JSON, server_default="{}"),
        sa.PrimaryKeyConstraint("run_id", "layer"),
    )

    op.create_table(
        "findings",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("layer", sa.Text, nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("citation_ordinal", sa.Integer),
        sa.Column("quote_ordinal", sa.Integer),
        sa.Column("span_start", sa.Integer),
        sa.Column("span_end", sa.Integer),
        sa.Column("evidence", sa.JSON, server_default="{}"),
        # 'deterministic' vs 'llm'. The UI renders these differently; that separation
        # is how a user can see which findings are machine-checkable ground truth.
        sa.Column("source", sa.Text, nullable=False, server_default="deterministic"),
    )
    op.create_index("ix_findings_run", "findings", ["run_id"])

    # Full prompt/response provenance. This table is the auditor's own audit trail:
    # every judge verdict remains inspectable after the fact.
    op.create_table(
        "judge_calls",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, server_default="v1"),
        sa.Column("request", sa.JSON, server_default="{}"),
        sa.Column("response_raw", sa.Text),
        sa.Column("parsed", sa.JSON),
        sa.Column("parse_path", sa.Text),
        sa.Column("retries", sa.Integer, server_default="0"),
        sa.Column("latency_ms", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    for table in (
        "judge_calls", "findings", "layer_results", "run_quotes", "run_sources",
        "run_citations", "runs", "list_entries", "chunks", "document_summaries",
        "text_embeddings", "citation_resolutions", "document_paragraphs", "documents",
    ):
        op.drop_table(table)
