"""L1 -- citation existence and quote verification.

The fixture text is REAL: paragraphs [83], [115] and [116] of Spandeck Engineering (S)
Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37, taken verbatim from
``tests/corpus/2007_SGCA_37.html``. Fuzzy thresholds calibrated against invented prose
prove nothing; legal writing has its own register and its own punctuation.

Contract objects are constructed directly rather than imported from the extraction
module, so this suite compiles against the frozen contracts and not against another
workstream's code.
"""

from __future__ import annotations

import pytest

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedCitation,
    ExtractedQuote,
    Resolution,
    Span,
)
from verifier.contracts.documents import Paragraph, SourceDocument
from verifier.contracts.enums import (
    ChunkKind,
    CitationType,
    FetchStrategy,
    FindingCode,
    LayerStatus,
    ResolutionMethod,
    ResolutionStatus,
    Severity,
)
from verifier.contracts.layers import ExtractionResult, LayerInput
from verifier.layers.l1a_existence import CitationExistenceLayer, normalize

# --- the real judgment -------------------------------------------------------------

URL = "https://www.elitigation.sg/gd/s/2007_SGCA_37"
DOMAIN = "elitigation.sg"
CASE_NAME = "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency"
TITLE = "[2007] SGCA 37"

HEADING = f"{CASE_NAME} {TITLE}"

PARA_83 = (
    "Assuming a positive answer to the preliminary question of factual foreseeability "
    "and the first stage of the legal proximity test, a prima facie duty of care arises. "
    "Policy considerations should then be applied to the factual matrix to determine "
    "whether or not to negate this duty. Among the relevant policy considerations would "
    "be, for example, the presence of a contractual matrix which has clearly defined the "
    "rights and liabilities of the parties and the relative bargaining positions of the "
    "parties."
)
PARA_115 = (
    "To recapitulate: A single test to determine the existence of a duty of care should "
    "be applied regardless of the nature of the damage caused (ie, pure economic loss or "
    "physical damage). It could be that a more restricted approach is preferable for "
    "cases of pure economic loss but this is to be done within the confines of a single "
    "test. This test is a two-stage test, comprising of, first, proximity and, second, "
    "policy considerations. These two stages are to be approached with reference to the "
    "facts of decided cases although the absence of such cases is not an absolute bar "
    "against a finding of duty."
)
PARA_116 = (
    "In the circumstances, applying the two-stage test, we found that there was no duty "
    "of care as the appellant had contended for. As a result, we did not have to consider "
    "the additional issues of breach, causation and remoteness which had been canvassed "
    "before us. The appeal was dismissed with costs and with the usual consequential "
    "orders."
)
BODY = "\n\n".join([HEADING, PARA_83, PARA_115, PARA_116])


# --- builders ----------------------------------------------------------------------


def neutral_citation(
    ordinal: int = 0,
    *,
    raw: str = TITLE,
    court: str = "SGCA",
    year: int = 2007,
    number: int = 37,
    case_name: str | None = None,
) -> ExtractedCitation:
    return ExtractedCitation(
        ordinal=ordinal,
        raw_text=raw,
        citation_type=CitationType.NEUTRAL,
        span=Span(start=0, end=len(raw)),
        court=court,
        year=year,
        number=number,
        case_name=case_name,
    )


def report_citation(ordinal: int = 1, raw: str = "[2007] 4 SLR(R) 100") -> ExtractedCitation:
    return ExtractedCitation(
        ordinal=ordinal,
        raw_text=raw,
        citation_type=CitationType.REPORT,
        span=Span(start=0, end=len(raw)),
    )


def cluster_of(*members: ExtractedCitation, ordinal: int = 0) -> CitationCluster:
    return CitationCluster(ordinal=ordinal, members=members, span=Span(start=0, end=40))


def resolution(
    citation: ExtractedCitation,
    status: ResolutionStatus = ResolutionStatus.RESOLVED,
    **overrides: object,
) -> Resolution:
    fields: dict[str, object] = {
        "citation_key": citation.citation_key,
        "status": status,
        "method": ResolutionMethod.URL,
        "url": URL,
        "domain": DOMAIN,
        "title": TITLE if status is ResolutionStatus.RESOLVED else None,
        "confidence": 1.0 if status is ResolutionStatus.RESOLVED else 0.0,
    }
    fields.update(overrides)
    return Resolution(**fields)  # type: ignore[arg-type]


def spandeck_document(text: str = BODY) -> SourceDocument:
    return SourceDocument(
        id="doc-spandeck",
        source_url=URL,
        domain=DOMAIN,
        fetch_strategy=FetchStrategy.HTTP,
        exists=True,
        http_status=200,
        neutral_citation=TITLE,
        case_name=CASE_NAME,
        court="SGCA",
        year=2007,
        text=text,
        paragraphs=(
            Paragraph(ordinal=0, paragraph_number=None, kind=ChunkKind.HEADING, text=HEADING),
            Paragraph(ordinal=1, paragraph_number=83, kind=ChunkKind.BODY, text=PARA_83),
            Paragraph(ordinal=2, paragraph_number=115, kind=ChunkKind.BODY, text=PARA_115),
            Paragraph(ordinal=3, paragraph_number=116, kind=ChunkKind.BODY, text=PARA_116),
        ),
    )


#: ``documents`` is keyed by citation_key, exactly like ``resolutions``.
SPANDECK_KEY = neutral_citation().citation_key


def layer_input(
    *,
    clusters: tuple[CitationCluster, ...] = (),
    quotes: tuple[ExtractedQuote, ...] = (),
    resolutions: dict[str, Resolution] | None = None,
    explicit_domains: tuple[str, ...] = (),
    documents: dict[str, SourceDocument] | None = None,
) -> LayerInput:
    """The single-flight resolver populates ``documents`` alongside ``resolutions``, so
    the Spandeck judgment is on the input by default. Pass ``documents={}`` for the case
    where a citation resolved but its text never arrived."""
    return LayerInput(
        run_id="run-1",
        question="What is the test for a duty of care in Singapore?",
        ai_output="...",
        extraction=ExtractionResult(
            clusters=clusters, quotes=quotes, explicit_domains=explicit_domains
        ),
        resolutions=resolutions or {},
        documents={SPANDECK_KEY: spandeck_document()} if documents is None else documents,
    )


def codes(result) -> list[FindingCode]:
    return [f.code for f in result.findings]


def only(result, code: FindingCode):
    matches = [f for f in result.findings if f.code is code]
    assert matches, f"expected {code.value}, got {[c.value for c in codes(result)]}"
    return matches[0]


# --- citation existence: the hallucination defence ---------------------------------


async def test_fabricated_citation_fails():
    """The one and only citation-level FAIL: positive evidence of non-existence.

    eLitigation answers a fabricated neutral citation with HTTP 200 and a ~3.5kB
    soft-404 (F3), so the resolver -- not the status code -- carries the signal.
    """
    fake = neutral_citation(court="SGCA", year=2019, number=999, raw="[2019] SGCA 999")
    cluster = cluster_of(fake)
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster,),
            resolutions={
                fake.citation_key: resolution(
                    fake, ResolutionStatus.NOT_FOUND, url=None, domain=None
                )
            },
        )
    )
    assert result.status is LayerStatus.FAIL
    finding = only(result, FindingCode.CITATION_NOT_FOUND)
    assert finding.severity is Severity.FAIL
    assert finding.citation_ordinal == 0


@pytest.mark.parametrize(
    ("status", "code"),
    [
        # A report-only citation cannot be resolved at all: the index is full-text, so
        # searching it returns cases that CITE the report, never the case itself (F7).
        (ResolutionStatus.UNRESOLVABLE, FindingCode.CITATION_UNVERIFIED),
        # The login-walled source rejected us; the session expired.
        (ResolutionStatus.UNAUTHENTICATED, FindingCode.SOURCE_UNAUTHENTICATED),
        # The source was down -- eLitigation's 819-byte maintenance page (F12). Failing
        # this would report every real Singapore case as hallucinated during an outage.
        (ResolutionStatus.ERROR, FindingCode.CITATION_UNVERIFIED),
        (ResolutionStatus.AMBIGUOUS, FindingCode.CITATION_AMBIGUOUS),
    ],
)
async def test_cannot_verify_is_never_fabricated(status, code):
    """The governing rule, exhaustively. Every 'we did not find out' state WARNs."""
    citation = report_citation() if status is ResolutionStatus.UNRESOLVABLE else neutral_citation()
    cluster = cluster_of(citation)
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster,),
            resolutions={citation.citation_key: resolution(citation, status)},
        )
    )
    assert result.status is LayerStatus.WARN
    assert not result.has_fail
    assert FindingCode.CITATION_NOT_FOUND not in codes(result)
    assert only(result, code).severity is Severity.WARN


async def test_report_only_citation_is_rescued_by_a_neutral_sibling():
    """A cluster is ONE reference written several ways.

    'Spandeck ... [2007] 4 SLR(R) 100; [2007] SGCA 37' is two citations but one thing to
    check, and the neutral form is checkable. This is what stops F7 turning every
    properly-cited authority yellow.
    """
    neutral = neutral_citation(0, case_name=CASE_NAME)
    report = report_citation(1)
    cluster = cluster_of(neutral, report)
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster,),
            resolutions={
                neutral.citation_key: resolution(neutral),
                report.citation_key: resolution(report, ResolutionStatus.UNRESOLVABLE),
            },
        )
    )
    assert result.status is LayerStatus.PASS
    assert result.findings == ()


async def test_missing_resolution_warns_and_never_fails():
    """Silence about a citation must not read as clearance -- but it is not a failure."""
    citation = neutral_citation()
    result = await CitationExistenceLayer().run(
        layer_input(clusters=(cluster_of(citation),), resolutions={})
    )
    assert result.status is LayerStatus.WARN
    assert only(result, FindingCode.CITATION_UNVERIFIED).severity is Severity.WARN


# --- right citation, wrong document ------------------------------------------------


async def test_title_mismatch_fails_as_wrong_document():
    """Real pages carry the neutral citation verbatim in <title> (F4): a free check."""
    citation = neutral_citation()
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={citation.citation_key: resolution(citation, title="[2017] SGCA 50")},
        )
    )
    assert result.status is LayerStatus.FAIL
    finding = only(result, FindingCode.RESOLVED_WRONG_DOC)
    assert finding.severity is Severity.FAIL
    assert finding.evidence.best_match_text == "[2017] SGCA 50"


async def test_absent_title_does_not_fail():
    """No title came back. That is not evidence of the wrong document."""
    citation = neutral_citation(case_name=CASE_NAME)
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={citation.citation_key: resolution(citation, title=None)},
        )
    )
    assert result.status is LayerStatus.PASS


async def test_parenthesised_court_suffix_matches_the_title():
    """SGHC(A) becomes SGHCA in the URL (F10); the title comparison must not care."""
    citation = neutral_citation(court="SGHC(A)", year=2021, number=5, raw="[2021] SGHC(A) 5")
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={citation.citation_key: resolution(citation, title="[2021] SGHC(A) 5")},
        )
    )
    assert result.status is LayerStatus.PASS


# --- right citation, wrong case name -----------------------------------------------


async def test_real_citation_attached_to_the_wrong_case_name_fails():
    """The citation exists. The case name belongs to a different case entirely."""
    citation = neutral_citation(case_name="Tan Cheng Bock v Attorney-General")
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={citation.citation_key: resolution(citation)},
        )
    )
    assert result.status is LayerStatus.FAIL
    finding = only(result, FindingCode.CITATION_CASE_NAME_MISMATCH)
    assert finding.severity is Severity.FAIL
    assert finding.evidence.threshold == 85.0
    assert finding.evidence.score is not None and finding.evidence.score < 85.0


async def test_correct_case_name_passes():
    citation = neutral_citation(case_name=CASE_NAME)
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={citation.citation_key: resolution(citation)},
        )
    )
    assert result.status is LayerStatus.PASS
    assert result.findings == ()


async def test_case_name_check_warns_rather_than_fails_when_the_text_is_unavailable():
    """No document, no check -- and no accusation either.

    The citation resolved, so it exists; only the cross-check against the judgment text
    could not run. That is a 'we did not find out', which WARNs. It must never become
    CITATION_CASE_NAME_MISMATCH, which asserts positive evidence we do not have.
    """
    citation = neutral_citation(case_name="Tan Cheng Bock v Attorney-General")
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={citation.citation_key: resolution(citation)},
            documents={},
        )
    )
    assert result.status is LayerStatus.WARN
    assert not result.has_fail
    assert FindingCode.CITATION_CASE_NAME_MISMATCH not in codes(result)
    finding = only(result, FindingCode.CITATION_UNVERIFIED)
    assert finding.severity is Severity.WARN
    assert finding.evidence.extra["pending"] == ["case name"]


async def test_a_fully_checked_citation_does_not_warn_about_a_missing_document():
    """Scoped, not blanket. A bare citation with no case name and no quotation asks
    nothing of the judgment text: the existence question is completely answered by the
    resolution, so warning here would be noise that teaches a reader to ignore warnings.
    """
    citation = neutral_citation()
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={citation.citation_key: resolution(citation)},
            documents={},
        )
    )
    assert result.status is LayerStatus.PASS
    assert result.findings == ()


# --- evidence, status and degraded operation ---------------------------------------


async def test_layer_is_not_applicable_when_there_is_nothing_to_check():
    result = await CitationExistenceLayer().run(layer_input())
    assert result.status is LayerStatus.NOT_APPLICABLE
    assert result.findings == ()


async def test_resolution_present_but_document_absent_degrades_without_going_silent():
    """The resolver answered but the text never arrived.

    Everything that does not need the text still runs -- fabrication is still caught --
    and everything that does is reported as unperformed rather than passed. The case
    name is never failed for our inability to read the judgment.
    """
    fake = neutral_citation(court="SGCA", year=2019, number=999, raw="[2019] SGCA 999")
    citation = neutral_citation(1, case_name=CASE_NAME)
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(fake), cluster_of(citation, ordinal=1)),
            resolutions={
                fake.citation_key: resolution(fake, ResolutionStatus.NOT_FOUND),
                citation.citation_key: resolution(citation),
            },
            documents={},
        )
    )
    assert result.status is LayerStatus.FAIL
    assert FindingCode.CITATION_NOT_FOUND in codes(result)
    assert FindingCode.CITATION_CASE_NAME_MISMATCH not in codes(result)

    unverified = only(result, FindingCode.CITATION_UNVERIFIED)
    assert unverified.severity is Severity.WARN
    assert unverified.citation_ordinal == 1
    assert unverified.evidence.extra["pending"] == ["case name"]
    assert result.detail["no_document"] == 1


async def test_an_empty_document_body_counts_as_no_document():
    """A judgment page that came back with no text verifies nothing. Matching a case
    name against "" would produce a confident 0 and a false accusation."""
    citation = neutral_citation(case_name=CASE_NAME)
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={citation.citation_key: resolution(citation)},
            documents={citation.citation_key: spandeck_document(text="")},
        )
    )
    assert not result.has_fail
    assert FindingCode.CITATION_CASE_NAME_MISMATCH not in codes(result)
    assert only(result, FindingCode.CITATION_UNVERIFIED).severity is Severity.WARN
    assert result.detail["no_document"] == 1


async def test_a_crash_reports_error_and_never_fails_the_run():
    """Inherited from BaseLayer, asserted here because it is a safety property: our own
    bug must not be rendered as the user's fabrication."""

    class Exploding(CitationExistenceLayer):
        async def _run(self, data):
            raise RuntimeError("boom")

    result = await Exploding().run(layer_input())
    assert result.status is LayerStatus.ERROR
    assert not result.has_fail


# --- normalisation -----------------------------------------------------------------


def test_normalisation_collapses_the_differences_that_break_exact_matching():
    messy = "  The “two–stage”   test — as\tstated. "
    clean = normalize(messy).text
    assert clean == 'the "two-stage" test - as stated.'


def test_normalisation_maps_every_character_back_to_the_source():
    source = "The “Two–Stage” Test"
    normalized = normalize(source)
    assert len(normalized.text) == len(normalized.origin)
    start = normalized.text.index("two-stage")
    assert normalized.source_slice(source, start, start + len("two-stage")) == "Two–Stage"


# -- what an UNRESOLVABLE citation is TOLD the reader ------------------------------
#
# One sentence used to cover every cause: "is a report-only citation, which the full-text
# index cannot resolve (F7)" -- printed for an sso.agc.gov.sg URL too, which is not a
# report citation and has nothing to do with the full-text index. Saying something
# specific we did not establish is a smaller error than a false FAIL, but it is the same
# kind, and not making it is this layer's whole discipline.


async def _unresolvable_message(detail: str | None) -> str:
    citation = report_citation()
    result = await CitationExistenceLayer().run(
        layer_input(
            clusters=(cluster_of(citation),),
            resolutions={
                citation.citation_key: resolution(
                    citation, ResolutionStatus.UNRESOLVABLE, detail=detail
                )
            },
        )
    )
    return only(result, FindingCode.CITATION_UNVERIFIED).message


async def test_a_report_only_citation_still_says_report_only() -> None:
    message = await _unresolvable_message("report_citation_not_resolvable")
    assert "report-only citation" in message
    assert "F7" in message


async def test_an_off_domain_url_is_not_called_a_report_only_citation() -> None:
    message = await _unresolvable_message("url_not_on_this_source")
    assert "report-only" not in message
    assert "outside the corpus" in message


async def test_a_host_with_no_adapter_says_so() -> None:
    """The detail carries a host suffix, so the lookup must match on the stem."""
    message = await _unresolvable_message("url_not_on_any_source:medium.com")
    assert "report-only" not in message
    assert "no adapter" in message


async def test_an_unknown_detail_gets_the_honest_default() -> None:
    for detail in (None, "something_nobody_wrote_a_sentence_for"):
        message = await _unresolvable_message(detail)
        assert "report-only" not in message
        assert "not evidence that it is wrong" in message


async def test_every_unresolvable_reason_stays_a_warn() -> None:
    """Only the sentence changes. None of these may drift toward a verdict."""
    from verifier.layers.l1a_existence import _UNRESOLVABLE_REASONS

    citation = report_citation()
    for detail in [*_UNRESOLVABLE_REASONS, None]:
        result = await CitationExistenceLayer().run(
            layer_input(
                clusters=(cluster_of(citation),),
                resolutions={
                    citation.citation_key: resolution(
                        citation, ResolutionStatus.UNRESOLVABLE, detail=detail
                    )
                },
            )
        )
        assert result.status is LayerStatus.WARN
        assert FindingCode.CITATION_NOT_FOUND not in codes(result)
        assert only(result, FindingCode.CITATION_UNVERIFIED).severity is Severity.WARN
        assert (
            "not evidence that it is wrong" in only(result, FindingCode.CITATION_UNVERIFIED).message
        )
