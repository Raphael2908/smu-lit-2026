"""Renumber the scoring layers from five to four, and drop the runs written under five.

Source trust used to be a layer of its own (L2), which pushed every later layer up by
one. It is now sub-check 1c inside Layer 1, so L3 grounding became L2, L4 responsiveness
became L3, and the L5 judge became L4.

WHY THIS DELETES DATA RATHER THAN REMAPPING IT.

``layer_results.layer`` and ``findings.layer`` are free text, so nothing in the database
rejects the old values -- which is precisely the problem. "L5" would now fail to parse
and take ``GET /v1/runs/{id}`` down with it, but "L2", "L3" and "L4" are all still VALID
and now name DIFFERENT layers. An old L3 row (grounding, score 0.61) would read back and
render as "Responsiveness: PASS 0.61". A 500 is a bug report; a green badge attached to
the wrong question is the exact failure this product exists to prevent.

Remapping is not available honestly: no row carries its vintage, so there is no way to
tell an old "L3" from a new one. Inventing a marker now cannot label rows already
written. And ``layer_results``' primary key is ``(run_id, layer)``, so old L1 and old L2
would both map onto new L1 and collide, needing a merge rule for two rows whose statuses
may disagree.

What is deleted is cheap to recreate. These rows are OBSERVATIONS of runs, not authored
data, and re-running the pipeline reproduces them. The expensive caches -- ``documents``,
``document_paragraphs``, ``chunks``, ``text_embeddings``, ``citation_resolutions`` -- are
keyed by citation and content, never by layer, so they are untouched here and a re-run
pays no fetch and no embedding.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
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
