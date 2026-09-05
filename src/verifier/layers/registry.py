"""Layer registry. All entries written up front so each workstream fills in one line.

Execution order is NOT this dict's order -- see pipeline/orchestrator.py. L1, L3 and
L4 run concurrently; L2 follows L1 because a bare citation has no domain until L1
resolves it; L5 runs only if every deterministic layer passed.
"""

from __future__ import annotations

from verifier.contracts.enums import Layer
from verifier.contracts.layers import LayerProtocol


def build_layer(layer: Layer) -> LayerProtocol:
    match layer:
        case Layer.L1_EXISTENCE:
            from verifier.layers.l1_existence import CitationExistenceLayer

            return CitationExistenceLayer()
        case Layer.L2_SOURCE_TRUST:
            from verifier.layers.l2_lists import SourceTrustLayer

            return SourceTrustLayer()
        case Layer.L3_GROUNDING:
            from verifier.layers.l3_alignment import SourceGroundingLayer

            return SourceGroundingLayer()
        case Layer.L4_RESPONSIVENESS:
            from verifier.layers.l4_responsiveness import ResponsivenessLayer

            return ResponsivenessLayer()
        case Layer.L5_JUDGE:
            from verifier.layers.l5_judge import FaithfulnessJudgeLayer

            return FaithfulnessJudgeLayer()
        case _:
            raise ValueError(f"No implementation registered for {layer}")


#: Layers that can fail the run and thereby skip the judge.
DETERMINISTIC_LAYERS: tuple[Layer, ...] = (
    Layer.L1_EXISTENCE,
    Layer.L2_SOURCE_TRUST,
    Layer.L3_GROUNDING,
    Layer.L4_RESPONSIVENESS,
)

#: Started together at t=0. L3 shares L1's single-flight document fetch rather than
#: waiting for L1's verdict: a citation can be fabricated while the argument is sound,
#: and a lawyer needs to see both.
PARALLEL_LAYERS: tuple[Layer, ...] = (
    Layer.L1_EXISTENCE,
    Layer.L3_GROUNDING,
    Layer.L4_RESPONSIVENESS,
)
