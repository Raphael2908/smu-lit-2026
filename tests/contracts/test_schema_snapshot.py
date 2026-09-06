"""The contract tripwire.

Every parallel workstream compiles against verifier.contracts. A silent change there
is the single most expensive kind of merge conflict, so it is made loud: this test
fails the build until the snapshot is regenerated deliberately.

    uv run python -m tests.contracts.test_schema_snapshot --update
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verifier.contracts.api import AcceptedResponse, ListEntryIn, RunEvent
from verifier.contracts.citations import (
    CitationCluster,
    ExtractedCitation,
    ExtractedProposition,
    ExtractedQuote,
    Resolution,
    StatuteReference,
)
from verifier.contracts.documents import Chunk, Paragraph, SourceDocument
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import ExtractionResult, LayerInput, LayerResult
from verifier.contracts.runs import RunState, VerifyRequest

SNAPSHOT = Path(__file__).parent / "schema_snapshot.json"

MODELS = [
    AcceptedResponse,
    ListEntryIn,
    RunEvent,
    ExtractedCitation,
    CitationCluster,
    ExtractedProposition,
    ExtractedQuote,
    Resolution,
    StatuteReference,
    Paragraph,
    SourceDocument,
    Chunk,
    Evidence,
    Finding,
    ExtractionResult,
    LayerInput,
    LayerResult,
    VerifyRequest,
    RunState,
]


def _current() -> dict:
    return {m.__name__: m.model_json_schema() for m in sorted(MODELS, key=lambda m: m.__name__)}


def test_contracts_have_not_drifted():
    if not SNAPSHOT.exists():
        pytest.skip("no snapshot yet; run with --update to create one")
    expected = json.loads(SNAPSHOT.read_text())
    actual = _current()
    changed = sorted(n for n in set(expected) | set(actual) if expected.get(n) != actual.get(n))
    assert not changed, (
        f"Frozen contracts changed: {changed}. This breaks every parallel workstream. "
        "If intentional, announce the contract change and regenerate the snapshot."
    )


if __name__ == "__main__":
    SNAPSHOT.write_text(json.dumps(_current(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT}")


def test_sub_layer_results_survive_the_json_column_they_ride_in():
    """``LayerResult.sub_results`` has no column in the frozen migration.

    It rides in ``layer_results.detail`` under ``_sub_results`` as plain JSON (see
    ``repos/runs.py``), so the pack/unpack has to be exact -- the panel renders Layer 1's
    per-sub-check status from whatever comes back out. The Pg repo itself needs a
    database and cannot be exercised offline, so the shape is pinned here instead.
    """
    from verifier.contracts.enums import LayerStatus, SubLayer
    from verifier.contracts.layers import SubLayerResult

    original = SubLayerResult(
        sub_layer=SubLayer.L1B_EXISTENCE,
        status=LayerStatus.FAIL,
        finding_count=2,
        detail={"clusters": 3, "not_found": 1},
    )
    packed = original.model_dump(mode="json")
    assert packed["sub_layer"] == "L1b", "the wire value is the string the panel keys on"
    assert SubLayerResult(**packed) == original


def test_a_finding_carries_its_sub_layer_through_the_evidence_column():
    """Same story for ``Finding.sub_layer``, packed under ``_sub_layer`` in evidence."""
    from verifier.contracts.enums import FindingCode, Layer, Severity, SubLayer
    from verifier.contracts.findings import Finding

    finding = Finding(
        id="run:L1c:domain:medium.com:SOURCE_GRAYLISTED",
        layer=Layer.L1_CITATION_INTEGRITY,
        sub_layer=SubLayer.L1C_SOURCE_TRUST,
        code=FindingCode.SOURCE_GRAYLISTED,
        severity=Severity.WARN,
        message="graylisted",
    )
    packed = str(finding.sub_layer)
    assert packed == "L1c"
    assert SubLayer(packed) is finding.sub_layer
