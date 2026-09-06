"""Move citedness out of L1 into the L0 preprocessing gate, and drop the runs before it.

L1 asked three questions -- 1a cited at all, 1b resolves, 1c trusted -- and the first of
them counted what an LLM extractor returned. A layer badged deterministic cannot contain
a model call, so citedness became L0's gate and the two that remain moved up: old 1b is
now 1a, old 1c is now 1b.

WHY THIS DELETES DATA RATHER THAN REMAPPING IT.

The same reason as 0002, arriving through a different column. ``"L1a"`` and ``"L1b"``
are both still VALID sub-layer values and both now name DIFFERENT checks. A stored 1a
(citedness: "the answer cites nothing") would render under the new vocabulary as
"Citation exists?", and a stored 1b (existence) as "Source trusted?" -- a badge attached
to the wrong question, which is the exact failure this product exists to prevent. Old
``"L1c"`` no longer parses at all.

Findings move too: ``OUTPUT_UNCITED`` and ``PROPOSITION_UNCITED`` were written with
``layer = "L1"`` and are now emitted under ``"L0"``, so an old run would show a citedness
finding filed under a layer that no longer raises it.

Remapping is not available honestly, for 0002's reason exactly: no row carries its
vintage, so there is no way to tell an old ``"L1a"`` from a new one.

What is deleted is cheap to recreate. These rows are OBSERVATIONS of runs, not authored
data. The expensive caches -- ``documents``, ``document_paragraphs``, ``chunks``,
``text_embeddings``, ``citation_resolutions`` -- are keyed by citation and content, never
by layer, so they are untouched and a re-run pays no fetch and no embedding.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # layer_results, findings, run_citations, run_quotes, run_sources and judge_calls
    # all declare ON DELETE CASCADE against runs, so this is the whole job.
    op.execute("DELETE FROM runs")


def downgrade() -> None:
    """Deliberately empty.

    The rows this migration removed cannot be reconstructed, and the schema itself did
    not change -- there is nothing structural to undo.
    """
