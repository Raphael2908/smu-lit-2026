"""L2 -- source trust, and the invariant that keeps it honest.

Sub-check 1c in isolation. The merged behaviour -- and the security property that a
whitelist can never clear a 1b fabrication finding -- lives in ``test_l1_composite.py``,
because that is where the two checks now meet.
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
)
from verifier.contracts.layers import ExtractionResult, LayerInput
from verifier.layers.l1ab_citations import CitationExistenceLayer
from verifier.layers.l1c_lists import SourceTrustLayer, normalize_domain
from verifier.repos.memory import InMemoryListRepo
from verifier.repos.seed_lists import SEED_ENTRIES, build_seeded_list_repo, seed_lists

BLACK = "ai-caselaw-generator.example"
GRAY = "wikipedia.org"
WHITE = "elitigation.sg"


# --- builders ----------------------------------------------------------------------


def citation(
    ordinal: int = 0,
    *,
    raw: str = "[2007] SGCA 37",
    court: str = "SGCA",
    year: int = 2007,
    number: int = 37,
) -> ExtractedCitation:
    return ExtractedCitation(
        ordinal=ordinal,
        raw_text=raw,
        citation_type=CitationType.NEUTRAL,
        span=Span(start=0, end=len(raw)),
        court=court,
        year=year,
        number=number,
    )


def cluster_of(*members: ExtractedCitation, ordinal: int = 0) -> CitationCluster:
    return CitationCluster(ordinal=ordinal, members=members, span=Span(start=0, end=40))


def resolved(
    source: ExtractedCitation, domain: str, status: ResolutionStatus = ResolutionStatus.RESOLVED
) -> Resolution:
    return Resolution(
        citation_key=source.citation_key,
        status=status,
        method=ResolutionMethod.URL,
        url=f"https://www.{domain}/gd/s/2007_SGCA_37",
        domain=domain,
        title="[2007] SGCA 37" if status is ResolutionStatus.RESOLVED else None,
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


async def list_repo(*entries: tuple[ListType, str]) -> InMemoryListRepo:
    repo = InMemoryListRepo()
    for list_type, pattern in entries:
        await repo.add(list_type, MatchType.DOMAIN, pattern, f"test {list_type.value}")
    return repo


def codes(result) -> list[FindingCode]:
    return [f.code for f in result.findings]


def only(result, code: FindingCode):
    matches = [f for f in result.findings if f.code is code]
    assert matches, f"expected {code.value}, got {[c.value for c in codes(result)]}"
    return matches[0]


# --- the three lists ---------------------------------------------------------------


async def test_blacklisted_domain_fails():
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(explicit_domains=(BLACK,))
    )
    assert result.status is LayerStatus.FAIL
    finding = only(result, FindingCode.SOURCE_BLACKLISTED)
    assert finding.severity is Severity.FAIL
    assert finding.evidence.extra["domain"] == BLACK


async def test_graylisted_domain_warns_but_passes():
    """A secondary source is a quality signal, not an error."""
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(explicit_domains=(GRAY,))
    )
    assert result.status is LayerStatus.WARN
    assert not result.has_fail
    assert only(result, FindingCode.SOURCE_GRAYLISTED).severity is Severity.WARN


async def test_whitelisted_domain_produces_no_finding():
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(explicit_domains=(WHITE,))
    )
    assert result.status is LayerStatus.PASS
    assert result.findings == ()
    assert result.detail["counts"]["white"] == 1


async def test_unknown_domain_is_info_and_coverage_stays_partial():
    """List silence is not clearance: our list is ~30 curated entries, not the web."""
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(explicit_domains=("some-firm-blog.example",))
    )
    assert result.status is LayerStatus.PASS
    assert only(result, FindingCode.SOURCE_UNKNOWN).severity is Severity.INFO
    assert result.detail["coverage"] == "partial"


async def test_coverage_is_complete_only_when_every_domain_matched():
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(explicit_domains=(WHITE, GRAY))
    )
    assert result.detail["coverage"] == "complete"


# --- THE invariant -----------------------------------------------------------------


async def test_1c_clears_a_trusted_domain_without_touching_1b():
    """1c suppresses its OWN findings on a whitelist hit, and nothing else.

    1b asks "does this citation exist?"; 1c asks "is this source trustworthy?". They are
    different questions and BOTH must pass. This is the unit-level half of the property;
    ``test_l1_composite.py`` asserts it survives the merge, which is where it could
    actually break.
    """
    fake = citation(raw="[2019] SGCA 999", year=2019, number=999)
    cluster = cluster_of(fake)
    resolutions = {fake.citation_key: resolved(fake, WHITE, status=ResolutionStatus.NOT_FOUND)}
    data = layer_input(clusters=(cluster,), resolutions=resolutions)

    l1 = await CitationExistenceLayer().run(data)
    l2 = await SourceTrustLayer(await build_seeded_list_repo()).run(data)

    # L2 is perfectly happy: the domain is the Singapore Courts' own judgment portal.
    assert l2.status is LayerStatus.PASS
    assert l2.findings == ()

    # L1 is not, and nothing L2 did touched it.
    assert l1.status is LayerStatus.FAIL
    assert FindingCode.CITATION_NOT_FOUND in codes(l1)

    # The run therefore still fails. 1c emits only its own findings and never rewrites,
    # downgrades or drops another sub-check's.
    combined = l1.findings + l2.findings
    assert any(f.severity is Severity.FAIL for f in combined)
    assert all(f.layer is l2.layer for f in l2.findings)
    assert l2.detail["whitelist_scope"] == "l2_only"


async def test_a_blacklisted_source_fails_even_when_the_citation_is_real():
    """The mirror image: 1b passing does not clear 1c either."""
    real = citation()
    resolutions = {real.citation_key: resolved(real, BLACK)}
    data = layer_input(clusters=(cluster_of(real),), resolutions=resolutions)

    l2 = await SourceTrustLayer(await build_seeded_list_repo()).run(data)
    assert l2.status is LayerStatus.FAIL
    assert only(l2, FindingCode.SOURCE_BLACKLISTED).citation_ordinal == 0


# --- the two input sets ------------------------------------------------------------


async def test_explicit_domains_are_checked_with_no_resolution_at_all():
    """A bare URL in the output carries its domain already -- nothing to fetch."""
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(explicit_domains=("https://en.wikipedia.org/wiki/Duty_of_care",))
    )
    assert only(result, FindingCode.SOURCE_GRAYLISTED).evidence.extra["origins"] == ["explicit"]


async def test_resolved_domains_come_from_l1():
    """A bare citation like [2007] SGCA 37 has no domain until L1 resolves it, which is
    why L2 runs after L1 rather than beside it."""
    source = citation()
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(
            clusters=(cluster_of(source),),
            resolutions={source.citation_key: resolved(source, BLACK)},
        )
    )
    finding = only(result, FindingCode.SOURCE_BLACKLISTED)
    assert finding.evidence.extra["origins"] == ["resolved"]
    assert finding.evidence.extra["citation_ordinals"] == [0]
    assert result.detail["resolved_domains"] == [BLACK]


async def test_one_finding_per_domain_however_many_citations_share_it():
    first, second = citation(0), citation(1, raw="[2007] SGCA 38", number=38)
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(
            clusters=(cluster_of(first), cluster_of(second, ordinal=1)),
            resolutions={
                first.citation_key: resolved(first, BLACK),
                second.citation_key: resolved(second, BLACK),
            },
        )
    )
    findings = [f for f in result.findings if f.code is FindingCode.SOURCE_BLACKLISTED]
    assert len(findings) == 1
    # Ambiguous which span to highlight, so we highlight none and list both.
    assert findings[0].citation_ordinal is None
    assert findings[0].evidence.extra["citation_ordinals"] == [0, 1]


async def test_no_domains_at_all_is_not_applicable():
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(layer_input())
    assert result.status is LayerStatus.NOT_APPLICABLE
    assert result.detail["coverage"] == "partial"


# --- matching ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.elitigation.sg/gd/s/2007_SGCA_37", "www.elitigation.sg"),
        ("http://LAWNET.sg:8080/case", "lawnet.sg"),
        ("elitigation.sg/gd/s/2007_SGCA_37", "elitigation.sg"),
        ("  Wikipedia.org  ", "wikipedia.org"),
        ("", ""),
    ],
)
def test_domain_normalisation(raw, expected):
    assert normalize_domain(raw) == expected


async def test_subdomains_inherit_their_parent_entry():
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(explicit_domains=("en.wikipedia.org", "www.elitigation.sg"))
    )
    assert result.status is LayerStatus.WARN
    assert only(result, FindingCode.SOURCE_GRAYLISTED).evidence.extra["domain"] == (
        "en.wikipedia.org"
    )
    assert FindingCode.SOURCE_UNKNOWN not in codes(result)


async def test_glob_patterns_match_self_published_hosts():
    result = await SourceTrustLayer(await build_seeded_list_repo()).run(
        layer_input(explicit_domains=("some-lawyer.blogspot.com",))
    )
    assert only(result, FindingCode.SOURCE_GRAYLISTED)


async def test_a_more_specific_entry_beats_a_broader_one():
    repo = await list_repo((ListType.WHITE, "example.test"), (ListType.BLACK, "bad.example.test"))
    result = await SourceTrustLayer(repo).run(layer_input(explicit_domains=("bad.example.test",)))
    assert result.status is LayerStatus.FAIL


# --- the seed lists ----------------------------------------------------------------


async def test_the_layer_defaults_to_the_curated_seed_lists():
    """SourceTrustLayer() with no arguments must work: registry.build_layer takes no
    constructor arguments, and the demo must run with no database."""
    result = await SourceTrustLayer().run(layer_input(explicit_domains=(BLACK, GRAY, WHITE)))
    assert result.status is LayerStatus.FAIL
    assert set(codes(result)) == {
        FindingCode.SOURCE_BLACKLISTED,
        FindingCode.SOURCE_GRAYLISTED,
    }


async def test_seed_entries_cover_all_three_lists():
    by_type: dict[ListType, int] = {list_type: 0 for list_type in ListType}
    for entry in SEED_ENTRIES:
        by_type[entry.list_type] += 1
    assert by_type[ListType.WHITE] >= 5
    assert by_type[ListType.GRAY] >= 5
    assert by_type[ListType.BLACK] >= 5
    assert len(SEED_ENTRIES) >= 30


def test_every_blacklisted_domain_is_a_reserved_example_domain():
    """Shipping a real domain on a blocklist is an accusation. These are illustrations
    of the two shapes worth blocking, on the RFC 2606 reserved TLD."""
    for entry in SEED_ENTRIES:
        if entry.list_type is ListType.BLACK:
            assert entry.pattern.endswith(".example"), entry.pattern


async def test_seed_lists_writes_into_any_list_repo():
    repo = InMemoryListRepo()
    written = await seed_lists(repo)
    assert written == len(SEED_ENTRIES)
    assert len(await repo.all()) == len(SEED_ENTRIES)
    assert await repo.match("www.elitigation.sg") == (
        ListType.WHITE,
        "Singapore Courts judgment portal (primary)",
    )


async def test_seeding_twice_is_idempotent():
    """``make seed-lists`` runs on every deploy; it must not accumulate duplicates."""
    repo = InMemoryListRepo()
    await seed_lists(repo)
    assert await seed_lists(repo) == 0
    assert len(await repo.all()) == len(SEED_ENTRIES)
