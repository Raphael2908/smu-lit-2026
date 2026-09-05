"""L1a -- is the proposition supported by any authority at all?

The stage that runs before "does the citation exist". L1b and L1c only ever examine
authority the output actually offered, so an answer that states the law from memory and
cites nothing would otherwise pass the citation-integrity layer with no mark against it.

The severity split is the point of most of these tests. The FAIL is a COUNT over the
whole output -- law asserted, no authority anywhere -- with no attribution judgement in
it, which is what lets it sit at the deterministic tier and skip the judge. Per-
proposition findings, where attribution IS a judgement over prose that has no fixed
citation structure, only ever WARN.
"""

from __future__ import annotations

import pytest

from verifier.contracts.enums import FindingCode, LayerStatus, Severity
from verifier.contracts.layers import LayerInput
from verifier.extraction import extract
from verifier.layers.l1_existence import CitationExistenceLayer
from verifier.settings import Settings

SPANDECK = "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37"

UNCITED = (
    "The test for a duty of care in Singapore is a two-stage inquiry. "
    "It is well established that factual foreseeability is the threshold requirement."
)


def layer_input(ai_output: str, *, is_followup: bool = False) -> LayerInput:
    return LayerInput(
        run_id="run-l1a",
        question="What is the test for a duty of care in Singapore?",
        ai_output=ai_output,
        is_followup=is_followup,
        extraction=extract(ai_output),
    )


async def run(ai_output: str, *, is_followup: bool = False, **overrides):
    """Run L1 with no resolutions and no documents: L1a needs neither.

    That is itself worth pinning -- L1a is pure text, so it produces its verdict with
    no fetch, no network and no model, in the same budget as the L2a blacklist check.
    """
    layer = CitationExistenceLayer()
    if overrides:
        settings = Settings(**overrides)
        monkey = pytest.MonkeyPatch()
        monkey.setattr("verifier.layers.l1_existence.get_settings", lambda: settings)
        try:
            return await layer.run(layer_input(ai_output, is_followup=is_followup))
        finally:
            monkey.undo()
    return await layer.run(layer_input(ai_output, is_followup=is_followup))


def codes(result) -> list[FindingCode]:
    return [f.code for f in result.findings]


def l1a(result):
    """Just the L1a findings.

    These tests supply no resolutions, so any citation in the fixture legitimately
    draws an L1b CITATION_UNVERIFIED warning ("we did not check it"). That is correct
    behaviour and orthogonal to what is under test here.
    """
    return [f for f in result.findings if f.evidence.extra.get("stage") == "L1a"]


# --- the FAIL: law asserted, nothing cited anywhere --------------------------------


async def test_an_answer_that_cites_nothing_fails():
    """The hole L1a exists to close.

    Before this stage, an output with no citations produced NOT_APPLICABLE from L1 and
    from L3, so a confidently uncited legal answer reached the verdict on L4 and the
    judge alone.
    """
    result = await run(UNCITED)
    assert result.status is LayerStatus.FAIL
    assert codes(result) == [FindingCode.OUTPUT_UNCITED]
    assert result.findings[0].severity is Severity.FAIL


async def test_the_fail_carries_every_uncited_assertion_as_evidence():
    """One finding, all the spans. Repeating the same message per sentence would train
    a reader to skim exactly the layer we want them to read."""
    result = await run(UNCITED)
    extra = result.findings[0].evidence.extra
    assert extra["authorities"] == 0
    assert extra["uncited"] == 2
    assert len(extra["propositions"]) == 2
    assert extra["stage"] == "L1a"


async def test_a_followup_turn_warns_instead_of_failing():
    """ "What about the second limb?" answers a question whose authority was given in
    the previous turn. Demanding that the answer re-cite it would fail the single most
    common shape of real conversation -- and under fail-fast a false red is
    unrecoverable. Same reasoning as L4's follow-up downgrade."""
    result = await run(UNCITED, is_followup=True)
    assert result.status is LayerStatus.WARN
    assert codes(result) == [FindingCode.OUTPUT_UNCITED]
    assert result.findings[0].severity is Severity.WARN


async def test_a_single_specific_statute_is_enough_authority_to_avoid_the_fail():
    result = await run(
        "Under section 20 of the Building Control Act (Cap 29), a developer must obtain "
        "approval before commencing works."
    )
    assert result.status is LayerStatus.PASS
    assert codes(result) == []


async def test_a_vague_statutory_reference_is_not_authority():
    """ "Under the Act" names nothing. An output that never names the Act has supported
    nothing, and saying otherwise would let a verifier be talked out of its job by a
    determiner."""
    result = await run("Under the Act, a developer must obtain written approval first.")
    assert result.status is LayerStatus.FAIL
    assert codes(result) == [FindingCode.OUTPUT_UNCITED]


# --- the WARN: cited elsewhere, but not here ---------------------------------------


async def test_an_uncited_assertion_beside_a_cited_one_only_warns():
    """Attribution over prose is a heuristic, and a heuristic must not fail a run.

    Two scopes: the first cites, the second does not. The output as a whole has
    authority, so the count-based FAIL does not apply and what remains is a judgement
    call -- which stays a WARN.
    """
    result = await run(
        f"The Court of Appeal held that a single test governs: {SPANDECK}.\n\n"
        "It is well established that a duty arises whenever loss is foreseeable."
    )
    assert result.status is LayerStatus.WARN
    assert FindingCode.PROPOSITION_UNCITED in codes(result)
    assert FindingCode.OUTPUT_UNCITED not in codes(result)
    warned = [f for f in result.findings if f.code is FindingCode.PROPOSITION_UNCITED]
    assert all(f.severity is Severity.WARN for f in warned)


async def test_the_per_proposition_finding_can_be_dialled_down_to_info():
    """``L1A_UNCITED_SEVERITY=info`` makes them display-only, for a corpus where the
    classifier proves noisy. The FAIL is deliberately not configurable this way."""
    result = await run(
        f"The Court of Appeal held that a single test governs: {SPANDECK}.\n\n"
        "It is well established that a duty arises whenever loss is foreseeable.",
        L1A_UNCITED_SEVERITY="info",
    )
    assert [f.severity for f in l1a(result)] == [Severity.INFO]


async def test_a_citation_carried_across_a_paragraph_clears_its_assertions():
    """Coverage is generous on purpose: legal writing cites once and then discusses."""
    result = await run(
        f"In {SPANDECK} the Court of Appeal held that one test governs. "
        "The test for proximity is one of physical, circumstantial and causal closeness. "
        "It is well established that policy considerations come last."
    )
    assert l1a(result) == []


# --- not applicable ----------------------------------------------------------------


async def test_an_output_that_asserts_no_law_is_not_applicable():
    """A procedural answer asserts nothing and cites nothing. There is no citation
    integrity question to ask about it, and inventing one would make the layer fire on
    every non-legal turn."""
    result = await run(
        "Here's how I'd structure the review: read the contract first, then the "
        "correspondence. Let me know which document you want to start with."
    )
    assert result.status is LayerStatus.NOT_APPLICABLE


async def test_l1a_can_be_disabled_without_touching_the_rest_of_l1():
    result = await run(UNCITED, L1A_ENABLED=False)
    # Nothing cited, nothing quoted, and the one stage that had something to say is
    # switched off -- so the layer genuinely has no question to answer.
    assert result.status is LayerStatus.NOT_APPLICABLE
    assert codes(result) == []


# --- the layer keeps its other stages ----------------------------------------------


async def test_l1a_does_not_disturb_the_counts_the_panel_renders():
    result = await run(
        f"The Court of Appeal held that a single test governs: {SPANDECK}.\n\n"
        "It is well established that a duty arises whenever loss is foreseeable."
    )
    assert result.detail["propositions"] == 2
    assert result.detail["propositions_cited"] == 1
    assert result.detail["propositions_uncited"] == 1
    assert result.detail["authorities"] == 1
    assert set(result.detail["stages"]) == {"L1a", "L1b", "L1c"}


# --- the extractor is now a model, and a model can be down --------------------------


def degraded_input(ai_output: str, reason: str = "citation extractor timed out"):
    """The same output, extracted by something that did not run."""
    return LayerInput(
        run_id="run-l1a",
        question="What is the test for a duty of care in Singapore?",
        ai_output=ai_output,
        extraction=extract(ai_output).model_copy(
            update={"clusters": (), "statutes": (), "extractor_degraded": reason}
        ),
    )


async def test_a_degraded_extractor_never_fails_the_run():
    """THE safety property of putting a model in L0.

    Since L1a stopped counting regex matches, "zero authority" no longer means "the
    answer cited nothing" -- it can equally mean the extractor timed out or had no key.
    Failing on that would report an outage as a fabrication, which is the same mistake
    F12 caught with the eLitigation maintenance page, arriving by a new route.
    """
    result = await CitationExistenceLayer().run(degraded_input(UNCITED))
    assert FindingCode.OUTPUT_UNCITED not in codes(result)
    assert not any(f.severity is Severity.FAIL for f in result.findings)


async def test_a_degraded_extractor_still_reports_the_assertions_as_unsupported():
    """Silence would present an unchecked answer as a clean one."""
    result = await CitationExistenceLayer().run(degraded_input(UNCITED))
    assert FindingCode.PROPOSITION_UNCITED in codes(result)
    assert all(f.severity is Severity.WARN for f in l1a(result))


async def test_the_same_output_still_fails_when_the_extractor_did_run():
    """The guard above must turn on the degraded flag, not on the count being zero."""
    result = await run(UNCITED)
    assert FindingCode.OUTPUT_UNCITED in codes(result)


async def test_authority_the_parser_could_not_type_still_clears_the_fail():
    """An unenumerated report series is authority, so the answer is not uncited.

    It is never resolved -- see extraction/llm.py -- so this is the only place it can
    show up, and if it did not count here the feature would fail exactly the answers it
    was built to rescue.
    """
    data = LayerInput(
        run_id="run-l1a",
        question="What is the test for a duty of care in Singapore?",
        ai_output=UNCITED,
        extraction=extract(UNCITED).model_copy(update={"untyped": ("(2005) 3 SCC 123",)}),
    )
    result = await CitationExistenceLayer().run(data)
    assert FindingCode.OUTPUT_UNCITED not in codes(result)
