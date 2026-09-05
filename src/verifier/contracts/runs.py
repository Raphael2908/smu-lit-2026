"""Run state -- one schema for the 202 body, the poll response and every SSE payload.

One schema, three transports, one renderer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from verifier.contracts.citations import Resolution
from verifier.contracts.enums import Layer, RunStatus, Verdict, VerdictStage
from verifier.contracts.findings import Finding
from verifier.contracts.layers import LayerResult


class RunOptions(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Debug/eval escape hatch: run the judge even after a deterministic failure.
    #: Never set by the extension.
    force_judge: bool = False
    skip_judge: bool = False
    jurisdiction: str = "SG"


class VerifyRequest(BaseModel):
    """The extension's capture contract. This pair is the system's ONLY input --
    there is no API integration with the model under test, which is what makes the
    verifier model- and vendor-agnostic."""

    question: str = Field(min_length=1)
    ai_output: str = Field(min_length=1)
    #: Up to ~3 prior turns, so a follow-up question can be disambiguated.
    context: list[dict[str, str]] = Field(default_factory=list)
    is_followup: bool = False
    options: RunOptions = Field(default_factory=RunOptions)
    client: dict[str, str] = Field(default_factory=dict)
    #: sha256(question || ai_output); dedupes retries and identical re-verifies.
    idempotency_key: str | None = None


class Timings(BaseModel):
    extract_ms: int = 0
    deterministic_ms: int = 0
    judge_ms: int = 0
    total_ms: int = 0


class CacheStats(BaseModel):
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class RunState(BaseModel):
    """The single response schema. ``seq`` increments on every mutation so a poller
    can ask for deltas and an SSE client can replay from Last-Event-ID."""

    run_id: str
    seq: int = 0
    status: RunStatus = RunStatus.PENDING
    verdict: Verdict = Verdict.PENDING
    verdict_stage: VerdictStage | None = None
    is_final: bool = False

    #: True when a deterministic failure meant the judge was never consulted.
    #: The panel shows this explicitly -- it is the invariant made legible.
    short_circuited: bool = False
    short_circuit_reason: str | None = None

    question: str = ""
    ai_output: str = ""
    resolutions: dict[str, Resolution] = Field(default_factory=dict)
    layers: dict[Layer, LayerResult] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)

    timings: Timings = Field(default_factory=Timings)
    cache: CacheStats = Field(default_factory=CacheStats)
    cost_usd: float = 0.0
    errors: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    completed_at: datetime | None = None

    def failing(self) -> list[Finding]:
        return [f for f in self.findings if f.is_fail]

    def model_dump_event(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)
