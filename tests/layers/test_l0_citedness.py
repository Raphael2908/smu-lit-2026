"""L0's gate -- is the proposition supported by any authority at all?

The question that runs before "does the citation exist". L1's two sub-checks only ever
examine authority the output actually offered, so an answer that states the law from
memory and cites nothing would otherwise reach a verdict with no mark against it.

It lives in L0 rather than in L1 because it counts what an LLM extractor returned, and a
layer badged deterministic must not contain a model call.

The severity split is the point of most of these tests. The FAIL is a COUNT over the
whole output -- law asserted, no authority anywhere -- with no attribution judgement in
it, which is what lets it stop the run before a single fetch. Per-proposition findings,
where attribution IS a judgement over prose that has no fixed citation structure, only
ever WARN.
"""

from __future__ import annotations

import pytest

from verifier.contracts.enums import FindingCode, Layer, LayerStatus, Severity
from verifier.contracts.layers import ExtractionResult, LayerInput
from verifier.extraction import extract
from verifier.layers.l0_preprocessing import PreprocessingLayer
from verifier.settings import Settings

SPANDECK = "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37"

UNCITED = (
    "The test for a duty of care in Singapore is a two-stage inquiry. "
    "It is well established that factual foreseeability is the threshold requirement."
)


def layer_input(
    ai_output: str, *, is_followup: bool = False, degraded: str | None = None
) -> LayerInput:
    extraction = extract(ai_output)
    if degraded is not None:
        extraction = extraction.model_copy(update={"extractor_degraded": degraded})
    return LayerInput(
        run_id="run-l0",
        question="What is the test for a duty of care in Singapore?",
        ai_output=ai_output,
        is_followup=is_followup,
        extraction=extraction,
    )


async def run(
    ai_output: str, *, is_followup: bool = False, degraded: str | None = None, **overrides
):
    """Run L0 with no resolutions and no documents: the gate needs neither.

    That is itself worth pinning -- the gate is pure with respect to the extraction it
    is handed, so it reaches its verdict with no fetch and no network of its own.
    """
    layer = PreprocessingLayer()
    data = layer_input(ai_output, is_followup=is_followup, degraded=degraded)
    if overrides:
        settings = Settings(**overrides)
        monkey = pytest.MonkeyPatch()
        monkey.setattr("verifier.layers.l0_preprocessing.get_settings", lambda: settings)
        try:
            return await layer.run(data)
        finally:
            monkey.undo()
    return await layer.run(data)


def codes(result) -> list[FindingCode]:
    return [f.code for f in result.findings]


def l0(result):
    """Every finding L0 raised. The layer has no sub-checks, so this is all of them."""
    return list(result.findings)


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
    assert result.findings[0].sub_layer is None


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
    """``L0_UNCITED_SEVERITY=info`` makes them display-only, for a corpus where the
    classifier proves noisy. The FAIL is deliberately not configurable this way."""
    result = await run(
        f"The Court of Appeal held that a single test governs: {SPANDECK}.\n\n"
        "It is well established that a duty arises whenever loss is foreseeable.",
        L0_UNCITED_SEVERITY="info",
    )
    assert [f.severity for f in l0(result)] == [Severity.INFO]


async def test_a_citation_carried_across_a_paragraph_clears_its_assertions():
    """Coverage is generous on purpose: legal writing cites once and then discusses."""
    result = await run(
        f"In {SPANDECK} the Court of Appeal held that one test governs. "
        "The test for proximity is one of physical, circumstantial and causal closeness. "
        "It is well established that policy considerations come last."
    )
    assert l0(result) == []


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


async def test_the_gate_can_be_disabled():
    result = await run(UNCITED, L0_CITEDNESS_ENABLED=False)
    # Nothing cited, nothing asserted that we are willing to look at -- the gate has no
    # question to answer, and NOT_APPLICABLE is not a pass.
    assert result.status is LayerStatus.NOT_APPLICABLE
    assert codes(result) == []


# --- what the panel renders ---------------------------------------------------------


async def test_the_gate_reports_the_counts_the_panel_renders():
    result = await run(
        f"The Court of Appeal held that a single test governs: {SPANDECK}.\n\n"
        "It is well established that a duty arises whenever loss is foreseeable."
    )
    assert result.detail["propositions"] == 2
    assert result.detail["propositions_cited"] == 1
    assert result.detail["propositions_uncited"] == 1
    assert result.detail["authorities"] == 1
    # The citation LIST, not just a count: the point of a model here is that a reader
    # can see what it decided the answer cited.
    assert [c["text"] for c in result.detail["citations"]] == ["[2007] SGCA 37"]


async def test_the_gate_has_no_sub_checks():
    """L0 asks one question. Only L1 reports sub-results."""
    result = await run(UNCITED)
    assert result.sub_results == ()
    assert all(f.sub_layer is None for f in result.findings)
    assert all(f.layer is Layer.L0_PREPROCESSING for f in result.findings)


# --- the extractor is a model, and a model can be down ------------------------------


def degraded_input(ai_output: str, reason: str = "citation extractor timed out"):
    """The same output, extracted by something that did not run."""
    return LayerInput(
        run_id="run-l0",
        question="What is the test for a duty of care in Singapore?",
        ai_output=ai_output,
        extraction=extract(ai_output).model_copy(
            update={"clusters": (), "statutes": (), "extractor_degraded": reason}
        ),
    )


async def test_a_degraded_extractor_fails_the_run():
    """A DELIBERATE REVERSAL, and the most consequential line in this file.

    L0 used to decline to fail when the extractor had not run: "zero authority" could
    mean the answer cited nothing OR that Haiku timed out, and reporting an outage as a
    fabrication is the F12 mistake arriving by a new route.

    The gate now fails either way. The reasoning is that a preprocessing step which did
    not run leaves NOTHING downstream with anything to check, so a run that continued
    would report a clean result over an answer nobody read -- which is the more dangerous
    of the two errors. The cost is real and is recorded in todo.md bug 5: an OpenRouter
    hiccup now reds a correct answer, and under fail-fast that red is unrecoverable.

    What survives the reversal is the DISTINCTION. See the next test.
    """
    result = await PreprocessingLayer().run(degraded_input(UNCITED))
    assert result.status is LayerStatus.FAIL
    assert any(f.severity is Severity.FAIL for f in result.findings)


async def test_an_outage_is_never_reported_as_an_uncited_answer():
    """The half of the old guarantee that is NOT negotiable.

    Both states stop the run, so the verdict cannot tell them apart -- which makes the
    finding CODE the only thing that can. "You cited nothing" is an accusation about a
    lawyer's work; "we could not read this" is a confession about ours. Filing the second
    under the first would print a fabrication verdict on a vendor's bad afternoon.
    """
    result = await PreprocessingLayer().run(degraded_input(UNCITED))
    assert codes(result) == [FindingCode.PREPROCESSING_FAILED]
    assert FindingCode.OUTPUT_UNCITED not in codes(result)
    assert "citation extractor timed out" in result.findings[0].message
    assert result.detail["extractor_degraded"] == "citation extractor timed out"


async def test_an_outage_fails_even_an_answer_that_cites_properly():
    """Not a paradox: nothing read the answer, so its citations were never seen.

    A run that passed here would be asserting something about a text it never parsed.
    """
    cited = f"The Court of Appeal held that one test governs: {SPANDECK}."
    result = await PreprocessingLayer().run(degraded_input(cited))
    assert result.status is LayerStatus.FAIL
    assert codes(result) == [FindingCode.PREPROCESSING_FAILED]


async def test_the_same_output_fails_differently_when_the_extractor_did_run():
    """The two paths must not converge on one code."""
    result = await run(UNCITED)
    assert codes(result) == [FindingCode.OUTPUT_UNCITED]


async def test_authority_the_parser_could_not_type_still_clears_the_fail():
    """An unenumerated report series is authority, so the answer is not uncited.

    It is never resolved -- see extraction/llm.py -- so this is the only place it can
    show up, and if it did not count here the feature would fail exactly the answers it
    was built to rescue.
    """
    data = LayerInput(
        run_id="run-l0",
        question="What is the test for a duty of care in Singapore?",
        ai_output=UNCITED,
        extraction=extract(UNCITED).model_copy(update={"untyped": ("(2005) 3 SCC 123",)}),
    )
    result = await PreprocessingLayer().run(data)
    assert FindingCode.OUTPUT_UNCITED not in codes(result)


async def test_an_empty_extraction_with_no_flag_is_still_a_coherent_run():
    """``ExtractionResult()`` on its own is not an outage.

    The orchestrator folds a genuine extraction error into ``extractor_degraded`` before
    the gate ever sees it, so a bare empty extraction here means exactly what it says:
    an answer with nothing in it to check.
    """
    result = await PreprocessingLayer().run(
        LayerInput(run_id="run-l0", question="q", ai_output="ok", extraction=ExtractionResult())
    )
    assert result.status is LayerStatus.NOT_APPLICABLE
    assert result.findings == ()
