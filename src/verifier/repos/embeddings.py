"""Postgres ``EmbeddingRepo`` -- the content-hash keyed embedding cache.

This is the scalability story: the second query that touches a given judgment pays
nothing. The cache key is the sha256 of the FULL embed input (summary + heading path +
text), so changing the summary prompt correctly invalidates it.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from verifier.logging import get_logger
from verifier.repos.models import TextEmbedding
from verifier.repos.session import session_scope

log = get_logger(__name__)


def _as_floats(vec: Any) -> list[float]:
    """pgvector returns a numpy array when numpy is installed and a list otherwise.
    Normalise so callers never have to care which."""
    if vec is None:
        return []
    return [float(x) for x in vec]


class PgEmbeddingRepo:
    """Satisfies ``repos.base.EmbeddingRepo``."""

    async def get_many(self, model: str, input_hashes: list[str]) -> dict[str, list[float]]:
        if not input_hashes:
            return {}
        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(TextEmbedding.input_sha256, TextEmbedding.embedding).where(
                        TextEmbedding.model == model,
                        TextEmbedding.input_sha256.in_(list(set(input_hashes))),
                    )
                )
            ).all()
        return {h: _as_floats(vec) for h, vec in rows}

    async def put_many(
        self, model: str, vectors: dict[str, list[float]], document_id: str | None = None
    ) -> None:
        if not vectors:
            return
        doc_key: uuid.UUID | None = None
        if document_id:
            try:
                doc_key = uuid.UUID(document_id)
            except (ValueError, AttributeError, TypeError):
                doc_key = None

        rows = [
            {
                "model": model,
                "input_sha256": h,
                "dim": len(vec),
                "embedding": list(vec),
                "document_id": doc_key,
            }
            for h, vec in vectors.items()
            if vec
        ]
        if not rows:
            return
        async with session_scope() as s:
            stmt = pg_insert(TextEmbedding).values(rows)
            # A concurrent worker may have cached the same chunk. First writer wins;
            # the vectors are identical anyway because the key IS the content hash.
            await s.execute(stmt.on_conflict_do_nothing(constraint="uq_embedding_model_input"))

    async def sample_background(
        self, model: str, limit: int, exclude_document_id: str | None = None
    ) -> list[list[float]]:
        """Chunks from OTHER cached judgments, for L3's contrastive margin.

        L3 scores ``max cos(claim, cited) - max cos(claim, BACKGROUND)``. That margin
        is only meaningful if the background is a fair sample of "an unrelated
        judgment". Two things therefore matter, and both are enforced here:

        1. ``exclude_document_id`` -- never sample from the document under test, or the
           margin measures the document against itself and collapses to ~0.
        2. The sample must SPAN SEVERAL DOCUMENTS. A plain ``LIMIT n`` returns the rows
           Postgres finds first, which in practice is one or two documents -- and if
           those happen to be on the query's own area of law, every claim looks
           ungrounded. So take a per-document quota via a window function.
        """
        if limit <= 0:
            return []

        exclude_key: uuid.UUID | None = None
        if exclude_document_id:
            try:
                exclude_key = uuid.UUID(exclude_document_id)
            except (ValueError, AttributeError, TypeError):
                exclude_key = None

        async with session_scope() as s:
            where = [TextEmbedding.model == model, TextEmbedding.document_id.is_not(None)]
            if exclude_key is not None:
                where.append(TextEmbedding.document_id != exclude_key)

            n_docs = (
                await s.execute(
                    select(func.count(func.distinct(TextEmbedding.document_id))).where(*where)
                )
            ).scalar_one()
            if not n_docs:
                return []

            # Spread the quota evenly; ceil so a small corpus still fills the budget.
            per_doc = max(1, math.ceil(limit / n_docs))
            ranked = (
                select(
                    TextEmbedding.embedding.label("embedding"),
                    func.row_number()
                    .over(
                        partition_by=TextEmbedding.document_id,
                        # Deterministic ordering: a background that reshuffles between
                        # runs makes L3's margin irreproducible, and an irreproducible
                        # verdict is not evidence.
                        order_by=TextEmbedding.id,
                    )
                    .label("rn"),
                )
                .where(*where)
                .subquery()
            )
            rows = (
                await s.execute(
                    select(ranked.c.embedding).where(ranked.c.rn <= per_doc).limit(limit)
                )
            ).scalars()
            return [_as_floats(v) for v in rows]
