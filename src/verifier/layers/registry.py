"""Layer registry.

Execution order is NOT this dict's order -- see pipeline/orchestrator.py.

L0 gates: it runs first, alone, and a FAIL there ends the run before L1 starts. Every
SCORING layer then starts at t=0 -- with source trust folded into L1 as sub-check 1b,
nothing is left that has to wait for another layer's verdict. L4 runs only if every
deterministic layer passed.
"""

from __future__ import annotations

from verifier.contracts.enums import Layer
from verifier.contracts.layers import LayerProtocol


def build_layer(layer: Layer) -> LayerProtocol:
    match layer:
        case Layer.L0_PREPROCESSING:
            from verifier.layers.l0_preprocessing import PreprocessingLayer

            return PreprocessingLayer()
        case Layer.L1_CITATION_INTEGRITY:
            from verifier.layers.l1_citation_integrity import CitationIntegrityLayer

            return CitationIntegrityLayer()
        case Layer.L2_ALIGNMENT:
            from verifier.layers.l2_alignment import SourceGroundingLayer

            return SourceGroundingLayer()
        case Layer.L3_RESPONSIVENESS:
            from verifier.layers.l3_responsiveness import ResponsivenessLayer

            return ResponsivenessLayer()
        case Layer.L4_JUDGE:
            from verifier.layers.l4_judge import FaithfulnessJudgeLayer

            return FaithfulnessJudgeLayer()
        case _:
            raise ValueError(f"No implementation registered for {layer}")


#: Scoring layers that can fail the run and thereby skip the judge.
#:
#: L0 is deliberately absent. It can fail a run too, but it is not a peer of these: it
#: runs BEFORE them and its failure means they never execute at all. Including it here
#: would put a gate in a tuple whose whole meaning is "these three run together".
DETERMINISTIC_LAYERS: tuple[Layer, ...] = (
    Layer.L1_CITATION_INTEGRITY,
    Layer.L2_ALIGNMENT,
    Layer.L3_RESPONSIVENESS,
)

#: Started together at t=0, and identical to DETERMINISTIC_LAYERS on purpose. L2 shares
#: L1's single-flight document fetch rather than waiting for L1's verdict: a citation can
#: be fabricated while the argument is sound, and a lawyer needs to see both.
PARALLEL_LAYERS: tuple[Layer, ...] = DETERMINISTIC_LAYERS
