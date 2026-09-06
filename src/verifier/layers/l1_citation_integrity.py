"""L1 -- citation integrity. One layer, two sub-checks.

    1a  Does each citation resolve to a real document, and the right one?
    1b  Is the domain it resolved to one we trust?

They run in the order they depend on each other, and the layer publishes ONE result.
Splitting them into two rows would tell a reader the system has four deterministic
layers when it has three; ``LayerResult.sub_results`` reports each one's status instead,
so nothing is hidden and nothing is inflated.

NOTHING IN THIS LAYER CALLS A MODEL. That is the layer's whole claim, and it used to be
false: L1 also carried "does this answer cite anything at all?" as a sub-check 1a, and
that check counts what an LLM extractor returned. One non-deterministic sub-check
defeats the badge for the layer. Citedness is now L0's gate
(``layers/l0_preprocessing.py``) and the two checks that remain moved up a letter.

WHY 1b CANNOT LAUNDER A 1a FAILURE -- the invariant this module exists to hold.

A whitelist suppresses 1b's OWN findings and nothing else. If "whitelisted overrules
all" were implemented literally it would be a laundering hole: put elitigation.sg on the
whitelist and every fabricated eLitigation citation -- resolved from a real domain,
pointing at a document that does not exist -- would pass. Trust in a publisher is not
evidence about a document.

Merging two layers into one object is exactly when that guarantee gets lost, so it is
structural here rather than a promise. ``SourceTrustLayer`` receives a ``LayerInput``
and nothing else: no findings, no other sub-check's result. It is not capable of reading
a fabrication finding, so it cannot clear one. This module only ever CONCATENATES the
sub-checks' findings -- never filters, never rewrites -- and ``_assert_nothing_was_suppressed``
turns that into a runtime check rather than a review convention.

See ``tests/layers/test_l1_composite.py``.
"""

from __future__ import annotations

from verifier.contracts.enums import Layer, LayerStatus, SubLayer
from verifier.contracts.findings import Finding
from verifier.contracts.layers import LayerInput, LayerResult, SubLayerResult
from verifier.errors import ContractViolation
from verifier.layers.base import BaseLayer, status_from_findings
from verifier.layers.l1a_existence import CitationExistenceLayer
from verifier.layers.l1b_lists import SourceTrustLayer
from verifier.repos.base import ListRepo


class CitationIntegrityLayer(BaseLayer):
    """L1. Sequences 1a then 1b, and merges them into a single result.

    ``lists`` is optional so ``registry.build_layer`` can construct the layer with no
    arguments; the trust check then falls back to the curated seed lists, which means
    the layer is fully functional offline with no database.
    """

    layer = Layer.L1_CITATION_INTEGRITY

    def __init__(self, lists: ListRepo | None = None) -> None:
        #: 1a. Resolution and document identity.
        self.citations = CitationExistenceLayer()
        #: 1b. Deliberately given no way to see the above.
        self.trust = SourceTrustLayer(lists)

    async def _run(self, data: LayerInput) -> LayerResult:
        # ``.run`` rather than ``._run``: BaseLayer.run contains a crash per sub-check,
        # so a trust-list outage cannot erase 1a's fabrication findings. That
        # containment is load-bearing -- without it a raising 1b would take the whole
        # layer to ERROR with no findings at all, which is a laundering route opened by
        # a refactor rather than by anyone's intent.
        citations = await self.citations.run(data)
        trust = await self.trust.run(data)
        return self._merge(data, citations, trust)

    async def precheck_explicit_domains(self, data: LayerInput) -> LayerResult:
        """1b over the domains the output wrote out itself, before anything is fetched.

        The cheapest failure the system can produce: a blacklisted source is decided
        from text alone -- no HTTP request to a court website, no worker hop, no model
        tokens. Only domains the output named itself can be checked here; a bare
        citation like ``[2007] SGCA 37`` has no domain until 1a resolves it.

        The caller keeps this result ONLY if it fails, in which case the run stops and
        this is Layer 1's one and only result. On any other path it is discarded and
        ``_run`` recomputes 1b over the full explicit-plus-resolved domain set -- the
        two passes overlap on explicit domains, and keeping both is how a graylisted
        domain came to be reported twice.
        """
        trust = await self.trust.run(data)
        findings = trust.findings
        return LayerResult(
            layer=self.layer,
            status=status_from_findings(findings),
            findings=findings,
            sub_results=(
                # Said explicitly, because a blacklist FAIL ends the run here: 1a did not
                # pass, it never ran.
                SubLayerResult(sub_layer=SubLayer.L1A_EXISTENCE, status=LayerStatus.SKIPPED),
                _trust_sub_result(trust),
            ),
            detail={**trust.detail, "phase": "pre_fetch"},
        )

    def _merge(self, data: LayerInput, citations: LayerResult, trust: LayerResult) -> LayerResult:
        findings = citations.findings + trust.findings
        _assert_nothing_was_suppressed(data.run_id, citations, trust, findings)

        sub_results = tuple(citations.sub_results) + (_trust_sub_result(trust),)
        if citations.status is LayerStatus.ERROR:
            # BaseLayer.run reports ERROR with no sub_results, so say which check was
            # lost rather than dropping it silently from the report.
            sub_results = (
                SubLayerResult(sub_layer=SubLayer.L1A_EXISTENCE, status=LayerStatus.ERROR),
                _trust_sub_result(trust),
            )

        if not findings and _both_had_nothing_to_check(citations, trust):
            # Nothing was cited and no domain was named, so there is no citation
            # integrity question to ask. Whether the answer SHOULD have cited something
            # is L0's question, already asked before this layer ran.
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.NOT_APPLICABLE,
                sub_results=sub_results,
            )

        return LayerResult(
            layer=self.layer,
            status=status_from_findings(findings),
            findings=findings,
            sub_results=sub_results,
            # No score. Quote matching was the only numeric signal this layer ever had,
            # and inventing a replacement would put an unmeasured number in the same UI
            # slot as L2's calibrated cosine.
            detail={**citations.detail, "trust": trust.detail},
            cache_hits=citations.cache_hits + trust.cache_hits,
            cache_misses=citations.cache_misses + trust.cache_misses,
        )


def _trust_sub_result(trust: LayerResult) -> SubLayerResult:
    return SubLayerResult(
        sub_layer=SubLayer.L1B_SOURCE_TRUST,
        status=trust.status,
        finding_count=len(trust.findings),
        detail=dict(trust.detail),
    )


def _both_had_nothing_to_check(citations: LayerResult, trust: LayerResult) -> bool:
    inapplicable = {LayerStatus.NOT_APPLICABLE, LayerStatus.SKIPPED}
    return citations.status in inapplicable and trust.status in inapplicable


def _assert_nothing_was_suppressed(
    run_id: str,
    citations: LayerResult,
    trust: LayerResult,
    merged: tuple[Finding, ...],
) -> None:
    """Every sub-check's finding must appear verbatim in the merged tuple.

    The whitelist invariant restated as an assertion: this layer concatenates, and any
    future edit that filters -- to "resolve" a whitelisted domain against a fabrication
    finding, say -- fails here instead of silently clearing the run.
    """
    merged_ids = {f.id for f in merged}
    for source in (citations, trust):
        missing = [f.id for f in source.findings if f.id not in merged_ids]
        if missing:
            raise ContractViolation(
                f"L1 dropped findings while merging its sub-checks (run {run_id}): {missing}. "
                "The layer may only concatenate; a whitelist must never clear a 1a finding."
            )
