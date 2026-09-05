"""The layer interface contract. Every layer takes a LayerInput and returns a LayerResult."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from verifier.contracts.citations import CitationCluster, ExtractedQuote, Resolution
from verifier.contracts.enums import Layer, LayerStatus
from verifier.contracts.findings import Finding


class ExtractionResult(BaseModel):
    """L0 output: the work items every other layer consumes."""

    model_config = ConfigDict(frozen=True)

    clusters: tuple[CitationCluster, ...] = ()
    quotes: tuple[ExtractedQuote, ...] = ()
    #: Domains written out explicitly in the output (bare URLs, "according to x.com").
    #: These carry a domain already, so L2a can check them before anything is fetched.
    explicit_domains: tuple[str, ...] = ()


class LayerInput(BaseModel):
    """Everything a layer may read. Layers are pure with respect to this input:
    they never reach into the DB or another layer's state directly."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run_id: str
    question: str
    ai_output: str
    context: tuple[dict[str, str], ...] = ()
    #: Set when the question cannot stand alone ("why?", "what about the second limb?").
    #: L4 downgrades to WARN rather than FAIL -- under fail-fast a false red is
    #: unrecoverable, and follow-ups are the likeliest way to hit one.
    is_followup: bool = False

    extraction: ExtractionResult = Field(default_factory=ExtractionResult)
    #: Populated by the shared single-flight resolver. L1 and L3 both read it, so one
    #: fetch serves both and L3 never waits on L1's verdict.
    resolutions: dict[str, Resolution] = Field(default_factory=dict)


class LayerResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    layer: Layer
    status: LayerStatus
    findings: tuple[Finding, ...] = ()
    score: float | None = None
    duration_ms: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def has_fail(self) -> bool:
        return any(f.is_fail for f in self.findings)


class LayerProtocol(Protocol):
    """What every layer implements. Frozen -- streams code against this, not each other."""

    layer: Layer

    async def run(self, data: LayerInput) -> LayerResult: ...
