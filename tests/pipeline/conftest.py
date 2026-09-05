"""Stubs for the pipeline tests.

Deliberately self-contained: these tests construct their own layers rather than
importing L1-L4, so the invariant is proven against the CONTRACTS and stays green
regardless of what any other workstream is doing to its own module today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedCitation,
    Span,
)
from verifier.contracts.enums import (
    CitationType,
    FindingCode,
    FindingSource,
    Layer,
    LayerStatus,
    ListType,
    ResolutionStatus,
    Severity,
)
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import ExtractionResult, LayerInput, LayerResult
from verifier.contracts.runs import RunOptions, VerifyRequest

QUESTION = "What is the test for a duty of care in Singapore?"
ANSWER = (
    "The Court of Appeal set out a single two-stage test in Spandeck Engineering (S) "
    "Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37."
)


def finding(
    code: FindingCode,
    severity: Severity,
    *,
    layer: Layer = Layer.L1_EXISTENCE,
    message: str = "",
    source: FindingSource = FindingSource.DETERMINISTIC,
    ident: str | None = None,
    evidence: Evidence | None = None,
) -> Finding:
    return Finding(
        id=ident or f"test:{layer.value}:{code.value}",
        layer=layer,
        code=code,
        severity=severity,
        message=message or code.value,
        source=source,
        evidence=evidence or Evidence(),
    )


def l1_fabrication_finding() -> Finding:
    """The finding this whole system exists to produce: a citation that does not exist."""
    return finding(
        FindingCode.CITATION_NOT_FOUND,
        Severity.FAIL,
        layer=Layer.L1_EXISTENCE,
        message="[2019] SGCA 999 does not exist: eLitigation returned a soft-404.",
        ident="run:L1:CITATION_NOT_FOUND:0",
    )


@dataclass
class StubLayer:
    """A layer that returns exactly what a test tells it to, and records that it ran."""

    layer: Layer
    findings: tuple[Finding, ...] = ()
    status: LayerStatus | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    raises: Exception | None = None
    calls: int = 0
    seen: list[LayerInput] = field(default_factory=list)

    async def run(self, data: LayerInput) -> LayerResult:
        self.calls += 1
        self.seen.append(data)
        if self.raises is not None:
            raise self.raises
        status = self.status
        if status is None:
            if any(f.severity is Severity.FAIL for f in self.findings):
                status = LayerStatus.FAIL
            elif any(f.severity is Severity.WARN for f in self.findings):
                status = LayerStatus.WARN
            else:
                status = LayerStatus.PASS
        return LayerResult(
            layer=self.layer,
            status=status,
            findings=self.findings,
            detail=self.detail,
        )


@dataclass
class RecordingJudgeLayer:
    """An L5 stand-in that records whether it was invoked at all.

    ``calls == 0`` on a failing run is the proof the gate did its job: the judge was
    never given the chance to disagree.
    """

    layer: Layer = Layer.L5_JUDGE
    findings: tuple[Finding, ...] = ()
    calls: int = 0

    async def run(self, data: LayerInput) -> LayerResult:
        self.calls += 1
        status = LayerStatus.PASS if not self.findings else LayerStatus.WARN
        return LayerResult(layer=self.layer, status=status, findings=self.findings)


class StubListRepo:
    """Just enough of ``ListRepo`` for L2a."""

    def __init__(self, entries: dict[str, tuple[ListType, str]] | None = None) -> None:
        self.entries = entries or {}
        self.lookups: list[str] = []

    async def match(self, domain: str) -> tuple[ListType, str] | None:
        self.lookups.append(domain)
        return self.entries.get(domain.lower())


class CollectingRunRepo:
    def __init__(self) -> None:
        self.saved: list[Any] = []
        self.runs: dict[str, Any] = {}

    async def save(self, state: Any) -> Any:
        # Snapshot: RunState is mutated in place through the run, so storing the live
        # object would make every recorded save look identical to the last one.
        self.saved.append(state.model_copy(deep=True))
        self.runs[state.run_id] = state
        return state

    async def get(self, run_id: str) -> Any:
        return self.runs.get(run_id)

    async def create(self, state: Any) -> Any:
        return await self.save(state)


def make_extraction(*, domains: tuple[str, ...] = (), citations: int = 0) -> ExtractionResult:
    clusters = []
    for index in range(citations):
        member = ExtractedCitation(
            ordinal=index,
            raw_text=f"[2007] SGCA {37 + index}",
            citation_type=CitationType.NEUTRAL,
            span=Span(start=0, end=15),
            court="SGCA",
            year=2007,
            number=37 + index,
        )
        clusters.append(
            CitationCluster(ordinal=index, members=(member,), span=Span(start=0, end=15))
        )
    return ExtractionResult(
        clusters=tuple(clusters),
        explicit_domains=domains,
    )


def make_request(
    *,
    options: RunOptions | None = None,
    question: str = QUESTION,
    ai_output: str = ANSWER,
) -> VerifyRequest:
    return VerifyRequest(
        question=question,
        ai_output=ai_output,
        options=options or RunOptions(),
    )


def extractor_for(extraction: ExtractionResult):
    def _extract(_text: str) -> ExtractionResult:
        return extraction

    return _extract


@pytest.fixture
def resolution_factory():
    from verifier.contracts.citations import Resolution

    def _make(key: str, *, domain: str = "www.elitigation.sg") -> Resolution:
        return Resolution(
            citation_key=key,
            status=ResolutionStatus.RESOLVED,
            url=f"https://{domain}/gd/s/{key}",
            domain=domain,
        )

    return _make
