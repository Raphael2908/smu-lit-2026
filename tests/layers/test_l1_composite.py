"""Layer 1 as one layer: 1a, 1b and 1c merged into a single result.

The load-bearing test here is ``test_a_whitelist_cannot_clear_a_fabricated_citation``.
Everything else is table stakes; that one is the security property, and it is the test
that had to get STRONGER when source trust stopped being a layer of its own.

While they were separate layers the invariant held for free -- two objects, neither
holding the other's findings. Inside one composite that is no longer automatic, so these
tests assert it of the MERGED result.
"""

from __future__ import annotations

import pytest

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedCitation,
    Resolution,
    Span,
)
from verifier.contracts.enums import (
    CitationType,
    FindingCode,
    LayerStatus,
    ListType,
    MatchType,
    ResolutionMethod,
    ResolutionStatus,
    Severity,
    SubLayer,
)
from verifier.contracts.layers import ExtractionResult, LayerInput
from verifier.errors import ContractViolation
from verifier.layers.l1_citation_integrity import (
    CitationIntegrityLayer,
    _assert_nothing_was_suppressed,
)
from verifier.repos.memory import InMemoryListRepo
from verifier.repos.seed_lists import build_seeded_list_repo

pytestmark = pytest.mark.anyio

WHITE = "elitigation.sg"
BLACK = "ai-caselaw-generator.example"


def citation(raw: str = "[2007] SGCA 37", *, year: int = 2007, number: int = 37):
    return ExtractedCitation(
        ordinal=0,
        raw_text=raw,
        citation_type=CitationType.NEUTRAL,
        span=Span(start=0, end=len(raw)),
        court="SGCA",
        year=year,
        number=number,
    )


def cluster_of(*members: ExtractedCitation) -> CitationCluster:
    return CitationCluster(ordinal=0, members=members, span=Span(start=0, end=40))


def resolved(
    source: ExtractedCitation,
    domain: str,
    status: ResolutionStatus = ResolutionStatus.RESOLVED,
) -> Resolution:
    return Resolution(
        citation_key=source.citation_key,
        status=status,
        method=ResolutionMethod.URL,
        url=f"https://www.{domain}/gd/s/x",
        domain=domain,
        title=source.raw_text if status is ResolutionStatus.RESOLVED else None,
    )


def layer_input(
    *,
    clusters: tuple[CitationCluster, ...] = (),
    resolutions: dict[str, Resolution] | None = None,
    explicit_domains: tuple[str, ...] = (),
) -> LayerInput:
    return LayerInput(
        run_id="run-1",
        question="q",
        ai_output="...",
        extraction=ExtractionResult(clusters=clusters, explicit_domains=explicit_domains),
        resolutions=resolutions or {},
    )


def sub(result, which: SubLayer):
    return next(r for r in result.sub_results if r.sub_layer is which)


# --- the security property ---------------------------------------------------------


async def test_a_whitelist_cannot_clear_a_fabricated_citation():
    """The laundering hole this layer's structure exists to close.

    ``elitigation.sg`` is the Singapore Courts' own judgment portal and is whitelisted,
    so 1c is perfectly happy. The citation resolves from that trusted domain to a
    document that does not exist, so 1b is not. Trust in a publisher is not evidence
    about a document, and now that both live in ONE layer, the merged result has to
    keep saying so.
    """
    fake = citation("[2019] SGCA 999", year=2019, number=999)
    data = layer_input(
        clusters=(cluster_of(fake),),
        resolutions={fake.citation_key: resolved(fake, WHITE, ResolutionStatus.NOT_FOUND)},
    )

    result = await CitationIntegrityLayer(await build_seeded_list_repo()).run(data)

    # 1c cleared it, and cleared ONLY itself.
    assert sub(result, SubLayer.L1C_SOURCE_TRUST).status is LayerStatus.PASS
    assert sub(result, SubLayer.L1C_SOURCE_TRUST).finding_count == 0

    # 1b did not, and the whole layer fails with it.
    assert sub(result, SubLayer.L1B_EXISTENCE).status is LayerStatus.FAIL
    assert result.status is LayerStatus.FAIL
    assert FindingCode.CITATION_NOT_FOUND in [f.code for f in result.findings]
    assert any(f.severity is Severity.FAIL for f in result.findings)


async def test_the_merge_may_only_concatenate():
    """A future edit that filters findings must fail loudly, not clear a run quietly."""
    layer = CitationIntegrityLayer(await build_seeded_list_repo())
    fake = citation("[2019] SGCA 999", year=2019, number=999)
    data = layer_input(
        clusters=(cluster_of(fake),),
        resolutions={fake.citation_key: resolved(fake, WHITE, ResolutionStatus.NOT_FOUND)},
    )

    citations = await layer.citations.run(data)
    trust = await layer.trust.run(data)
    assert citations.findings, "the fixture must produce a 1b finding to drop"

    # The merge as it stands keeps everything, so the tripwire is silent.
    assert layer._merge(data, citations, trust).status is LayerStatus.FAIL  # noqa: SLF001

    # And a merge that dropped 1b's findings -- exactly what "whitelisted overrules
    # all" would look like if anyone ever built it -- raises instead of quietly
    # clearing the run.
    with pytest.raises(ContractViolation, match="may only concatenate"):
        _assert_nothing_was_suppressed(data.run_id, citations, trust, trust.findings)


async def test_a_blacklisted_source_fails_even_when_the_citation_is_real():
    """The mirror image: 1b passing does not clear 1c either."""
    real = citation()
    data = layer_input(
        clusters=(cluster_of(real),),
        resolutions={real.citation_key: resolved(real, BLACK)},
    )

    result = await CitationIntegrityLayer(await build_seeded_list_repo()).run(data)

    assert result.status is LayerStatus.FAIL
    assert sub(result, SubLayer.L1C_SOURCE_TRUST).status is LayerStatus.FAIL
    assert FindingCode.SOURCE_BLACKLISTED in [f.code for f in result.findings]


# --- one layer, three reported sub-checks ------------------------------------------


async def test_every_finding_is_tagged_with_the_sub_check_that_raised_it():
    real = citation()
    data = layer_input(
        clusters=(cluster_of(real),),
        resolutions={real.citation_key: resolved(real, "wikipedia.org")},
        explicit_domains=("https://en.wikipedia.org/wiki/Duty_of_care",),
    )

    result = await CitationIntegrityLayer(await build_seeded_list_repo()).run(data)

    assert [r.sub_layer for r in result.sub_results] == [
        SubLayer.L1A_CITEDNESS,
        SubLayer.L1B_EXISTENCE,
        SubLayer.L1C_SOURCE_TRUST,
    ]
    assert result.findings, "the graylisted domain should have been reported"
    assert all(f.sub_layer is not None for f in result.findings)
    # And the counts on each sub-result agree with the findings actually tagged to it.
    for reported in result.sub_results:
        tagged = [f for f in result.findings if f.sub_layer is reported.sub_layer]
        assert reported.finding_count == len(tagged)


async def test_the_layer_carries_no_score():
    """Quote matching was L1's only number, and it is gone.

    Nothing replaces it: an unmeasured 0-1 for "citation integrity" would sit in the
    same panel slot as L2's calibrated cosine and read as though it meant the same kind
    of thing.
    """
    real = citation()
    result = await CitationIntegrityLayer(await build_seeded_list_repo()).run(
        layer_input(
            clusters=(cluster_of(real),),
            resolutions={real.citation_key: resolved(real, WHITE)},
        )
    )
    assert result.score is None


async def test_nothing_asserted_and_nothing_cited_is_not_applicable():
    result = await CitationIntegrityLayer(await build_seeded_list_repo()).run(layer_input())
    assert result.status is LayerStatus.NOT_APPLICABLE
    assert result.findings == ()


# --- containment: one sub-check crashing must not erase another's findings ----------


class ExplodingLists(InMemoryListRepo):
    async def match(self, domain: str):
        raise RuntimeError("trust list is down")


async def test_a_trust_list_outage_cannot_erase_a_fabrication_finding():
    """The laundering route a naive merge would have opened.

    ``BaseLayer.run`` maps a crash to ERROR with NO findings. If the composite ran its
    sub-checks in one try block, a list-repo outage would take the whole layer to ERROR
    and 1b's CITATION_NOT_FOUND would vanish -- a fabricated citation passing because an
    unrelated service was down.
    """
    fake = citation("[2019] SGCA 999", year=2019, number=999)
    data = layer_input(
        clusters=(cluster_of(fake),),
        resolutions={fake.citation_key: resolved(fake, WHITE, ResolutionStatus.NOT_FOUND)},
    )

    result = await CitationIntegrityLayer(ExplodingLists()).run(data)

    assert FindingCode.CITATION_NOT_FOUND in [f.code for f in result.findings]
    assert result.status is LayerStatus.FAIL
    assert sub(result, SubLayer.L1C_SOURCE_TRUST).status is LayerStatus.ERROR


# --- the pre-fetch pass ------------------------------------------------------------


async def test_the_pre_fetch_pass_says_the_other_checks_did_not_run():
    """A blacklist FAIL ends the run, so 1a and 1b are SKIPPED -- not passed."""
    repo = InMemoryListRepo()
    await repo.add(ListType.BLACK, MatchType.DOMAIN, BLACK, "known fabricator")

    result = await CitationIntegrityLayer(repo).precheck_explicit_domains(
        layer_input(explicit_domains=(BLACK,))
    )

    assert result.status is LayerStatus.FAIL
    assert sub(result, SubLayer.L1C_SOURCE_TRUST).status is LayerStatus.FAIL
    assert sub(result, SubLayer.L1A_CITEDNESS).status is LayerStatus.SKIPPED
    assert sub(result, SubLayer.L1B_EXISTENCE).status is LayerStatus.SKIPPED
    assert result.detail["phase"] == "pre_fetch"
