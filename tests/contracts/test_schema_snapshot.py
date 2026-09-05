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
