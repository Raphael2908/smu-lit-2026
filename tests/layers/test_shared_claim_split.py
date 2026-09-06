"""One claim split, two consumers.

L2 and L3 both score the answer per claim, and each used to call
``chunk_output_claims`` for itself. Two costs, one of them not obvious:

* two summariser calls per run for one piece of work; and
* two possibly DIFFERENT claim lists. The splitter is a model, so "claim 3 is not
  grounded" (L2) and "claim 3 does not answer the question" (L3) could be about
  different sentences -- and a reader comparing the two rows would have no way to tell.

L0 now splits once and carries the list on ``LayerInput.claims``. The local fallback
stays in both layers, so a layer driven directly still works; these tests pin both
halves, because a fallback that silently always fired would restore the old behaviour
without any test noticing.
"""

from __future__ import annotations

from tests.semantic.fixtures import layer_input
from verifier.contracts.chunks import RawChunk
from verifier.contracts.enums import ChunkKind
from verifier.layers.l3_responsiveness import ResponsivenessLayer
from verifier.providers.mock.embeddings import MockEmbedder

QUESTION = "What is the test for the imposition of a duty of care in negligence in Singapore?"

ANSWER = (
    "Singapore applies a single test for the imposition of a duty of care in negligence. "
    "That single test is a two-stage test premised on proximity and policy. "
    "It is preceded by a preliminary requirement of factual foreseeability."
)


class CountingSummariser:
    """A summariser that records whether the layer asked it to split anything."""

    def __init__(self) -> None:
        self.splits = 0

    async def split_claims(self, text: str) -> list[str]:
        self.splits += 1
        return [s.strip() for s in text.split(". ") if s.strip()]

    async def summarise(self, text: str) -> str:  # pragma: no cover - unused here
        return text[:100]


def supplied_claims() -> tuple[RawChunk, ...]:
    """A claim list nothing in the layer could have produced for itself.

    Deliberately not a plausible split of ANSWER: if a layer ignored the supplied list
    and re-split, the marker text would be absent and the assertion would fail.
    """
    return (
        RawChunk(
            ordinal=0,
            kind=ChunkKind.CLAIM,
            text="SUPPLIED BY L0: Singapore applies a single duty of care test.",
            strategy="claims",
        ),
    )


async def test_l3_uses_the_claims_l0_supplied_and_does_not_re_split():
    summariser = CountingSummariser()
    layer = ResponsivenessLayer(embedder=MockEmbedder(), summariser=summariser, embedding_repo=None)

    result = await layer.run(
        layer_input(question=QUESTION, ai_output=ANSWER, claims=supplied_claims())
    )

    assert summariser.splits == 0, "the split L0 already paid for must not be paid again"
    assert result.score is not None


async def test_l3_still_splits_for_itself_when_nothing_was_supplied():
    """The fallback. A layer constructed directly has no L0 in front of it."""
    summariser = CountingSummariser()
    layer = ResponsivenessLayer(embedder=MockEmbedder(), summariser=summariser, embedding_repo=None)

    result = await layer.run(layer_input(question=QUESTION, ai_output=ANSWER))

    assert summariser.splits == 1
    assert result.score is not None
