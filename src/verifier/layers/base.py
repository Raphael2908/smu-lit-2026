"""Layer base class. Every layer is pure with respect to LayerInput."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from verifier.contracts.enums import Layer, LayerStatus, Severity, SubLayer
from verifier.contracts.findings import Finding
from verifier.contracts.layers import LayerInput, LayerResult, SubLayerResult


class BaseLayer(ABC):
    layer: Layer

    @abstractmethod
    async def _run(self, data: LayerInput) -> LayerResult: ...

    async def run(self, data: LayerInput) -> LayerResult:
        """Time the layer and make sure a crash never takes the run down.

        A layer that errors reports ERROR, not FAIL. Failing the output because our
        own code broke would be the worst kind of false positive.
        """
        started = time.perf_counter()
        try:
            result = await self._run(data)
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
            from verifier.contracts.enums import FindingCode

            return LayerResult(
                layer=self.layer,
                status=LayerStatus.ERROR,
                findings=(
                    Finding(
                        id=f"{data.run_id}:{self.layer.value}:error",
                        layer=self.layer,
                        code=FindingCode.LAYER_ERROR,
                        severity=Severity.WARN,
                        message=f"{self.layer.value} could not complete: {exc}",
                    ),
                ),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        return result.model_copy(
            update={"duration_ms": int((time.perf_counter() - started) * 1000)}
        )


def status_from_findings(findings: tuple[Finding, ...]) -> LayerStatus:
    if any(f.severity is Severity.FAIL for f in findings):
        return LayerStatus.FAIL
    if any(f.severity is Severity.WARN for f in findings):
        return LayerStatus.WARN
    return LayerStatus.PASS


def sub_result(
    sub_layer: SubLayer,
    findings: tuple[Finding, ...],
    *,
    ran: bool,
    detail: dict[str, Any] | None = None,
) -> SubLayerResult:
    """One sub-check's status, derived from the findings it actually tagged.

    Derived rather than tracked, so a sub-check's reported status cannot drift from the
    findings it emitted -- the same reason ``status_from_findings`` exists.

    ``ran`` distinguishes "looked and found nothing" from "had nothing to look at".
    NOT_APPLICABLE is not a pass: a sub-check that never ran must never read as
    clearance, which is the governing rule of this whole layer stack.
    """
    mine = tuple(f for f in findings if f.sub_layer is sub_layer)
    return SubLayerResult(
        sub_layer=sub_layer,
        status=status_from_findings(mine) if ran else LayerStatus.NOT_APPLICABLE,
        finding_count=len(mine),
        detail=detail or {},
    )
