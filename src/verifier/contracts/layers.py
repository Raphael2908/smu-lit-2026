"""The layer interface contract. Every layer takes a LayerInput and returns a LayerResult."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedProposition,
    ExtractedQuote,
    Resolution,
    StatuteReference,
)
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import Layer, LayerStatus, SubLayer
from verifier.contracts.findings import Finding


class ExtractionResult(BaseModel):
    """L0 output: the work items every other layer consumes."""

    model_config = ConfigDict(frozen=True)

    clusters: tuple[CitationCluster, ...] = ()
    quotes: tuple[ExtractedQuote, ...] = ()
    #: Sentences that assert law and therefore need authority. L1a checks whether each
    #: one has any, which is the question that precedes "does the citation exist": an
    #: output can be perfectly free of fabricated citations by citing nothing at all.
    propositions: tuple[ExtractedProposition, ...] = ()
    #: Statutory references. Authority for L1a, but never resolved against the judgment
    #: corpus -- see StatuteReference for why they are not clusters.
    statutes: tuple[StatuteReference, ...] = ()
    #: Domains written out explicitly in the output (bare URLs, "according to x.com").
    #: These carry a domain already, so 1c can check them before anything is fetched.
    explicit_domains: tuple[str, ...] = ()
    #: Citations the extractor found and the deterministic parser cannot type.
    #:
    #: An unenumerated report series ("(2005) 3 SCC 123"), a practice direction, a
    #: textbook. These are authority -- the answer offered them -- but they are not work
    #: items: resolving one means searching a Singapore judgment corpus for a phrase that
    #: is not in it, and zero hits is exactly what this system reads as fabrication (F6).
    #: So they COUNT for L1a and are never clustered, resolved or fetched. Kept as raw
    #: strings because there is, by definition, nothing parsed to keep.
    untyped: tuple[str, ...] = ()
    #: Why the citation extractor contributed nothing, when it did not run.
    #:
    #: Load-bearing, not diagnostic. L1a's FAIL asserts "this output cited nothing",
    #: which is only a statement about the output if the extractor actually looked. A
    #: timeout, a missing key or unparseable output leave the two indistinguishable, so
    #: L1a must not FAIL while this is set -- "cannot verify" is never "fabricated".
    extractor_degraded: str | None = None

    @property
    def authority_count(self) -> int:
        """Every distinct piece of authority the output offers, of any kind.

        L1a's FAIL turns on this being zero, which is why it is a plain count and not a
        judgement: no attribution, no thresholds, nothing to be wrong about beyond
        whether the text contains a citation at all.
        """
        return len(self.clusters) + sum(1 for s in self.statutes if s.specific) + len(self.untyped)


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
    #: Populated by the shared single-flight resolver, keyed by citation_key. L1 and
    #: L3 both read these, so one fetch serves both and L3 never waits on L1's verdict.
    resolutions: dict[str, Resolution] = Field(default_factory=dict)
    #: The fetched documents themselves, same keys as ``resolutions``.
    #:
    #: Carried on the input rather than looked up through a repo so that layers stay
    #: pure with respect to LayerInput -- a layer that reaches into the database is a
    #: layer that cannot be tested without one. Both L1 (quote and party verification)
    #: and L3 (grounding) need the document text, and the resolver has it already.
    documents: dict[str, SourceDocument] = Field(default_factory=dict)


class SubLayerResult(BaseModel):
    """One named check inside a layer, reported without splitting the layer.

    Carries NO findings. Findings stay flat on ``LayerResult.findings``, each tagged
    with its ``sub_layer``: a finding that existed in two places is the exact shape of
    bug ``aggregate.assert_additive`` exists to catch, and flattening keeps the
    orchestrator's ``state.findings.extend(result.findings)`` correct as written.
    """

    model_config = ConfigDict(frozen=True)

    sub_layer: SubLayer
    status: LayerStatus
    finding_count: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


class LayerResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    layer: Layer
    status: LayerStatus
    findings: tuple[Finding, ...] = ()
    #: Per-sub-check status, when a layer has named sub-checks. Empty for layers that
    #: ask a single question.
    sub_results: tuple[SubLayerResult, ...] = ()
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
