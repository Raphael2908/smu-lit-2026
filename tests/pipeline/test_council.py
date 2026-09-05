"""The council: independent seats, votes combined, and the failures that must not vote.

The cases that matter most here are not the happy path. They are the ones where a seat
is BROKEN -- it raised, it timed out, its verdict could not be read -- because the whole
risk of a panel is that a silent seat gets counted as agreement and an outage quietly
acquits an answer.
"""

from __future__ import annotations

import json

import pytest

from verifier.providers.base import JudgeResult, JudgeRubric
from verifier.providers.council import (
    CouncilJudge,
    combine,
)
from verifier.providers.openrouter_llm import (
    JudgeValidationError,
    validate_judge_object,
)


class _FakeJudge:
    """A seat with a fixed opinion. ``None`` scores mean it abstained."""

    provider = "fake"

    def __init__(self, model: str, correctness: int | None, completeness: int | None) -> None:
        self.model = model
        self._c = correctness
        self._m = completeness

    async def judge(self, *, system_prompt: str, payload: dict) -> JudgeResult:
        if self._c is None and self._m is None:
            return JudgeResult(passed=True, rubric=None, model=self.model, cost_usd=0.01)
        return JudgeResult(
            passed=self._c == 1 and self._m == 1,
            rubric=JudgeRubric(correctness=self._c, material_completeness=self._m),
            reasons=[f"{self.model} says so"],
            model=self.model,
            cost_usd=0.01,
        )


class _ExplodingJudge:
    provider = "fake"
    model = "boom"

    async def judge(self, *, system_prompt: str, payload: dict) -> JudgeResult:
        raise RuntimeError("provider is down")


def _council(spec: dict[str, tuple[int | None, int | None]], rule: str = "majority"):
    judges = {m: _FakeJudge(m, c, k) for m, (c, k) in spec.items()}
    return CouncilJudge(tuple(spec), rule=rule, judge_for=lambda m: judges[m])


# --- combine ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("votes", "rule", "expected"),
    [
        ([0, 1, 1], "majority", 1),
        ([0, 0, 1], "majority", 0),
        ([0, 1, 1], "any", 0),
        ([1, 1, 1], "any", 1),
        ([0, 0, 1], "unanimous", 1),
        ([0, 0, 0], "unanimous", 0),
        # An abstention is removed BEFORE the rule applies, so 0 vs 1 with one silent
        # seat is a tie among those who voted -- and a tie is not a majority.
        ([0, 1, None], "majority", 1),
        ([0, 0, None], "majority", 0),
    ],
)
def test_combine_rules(votes, rule, expected):
    assert combine(votes, rule) == expected


def test_combine_returns_none_when_every_seat_abstained():
    assert combine([None, None], "majority") is None


# --- the panel --------------------------------------------------------------------


@pytest.mark.anyio
async def test_majority_convicts_when_most_seats_convict():
    council = _council({"a": (0, 0), "b": (0, 1), "c": (1, 1)})
    result = await council.judge(system_prompt="p", payload={})
    assert result.rubric.correctness == 0  # 2 of 3 said 0
    assert result.rubric.material_completeness == 1  # 1 of 3 said 0
    assert result.passed is False


@pytest.mark.anyio
async def test_lone_dissenter_is_outvoted_under_majority():
    """The panel's central trade, pinned so it cannot change silently.

    One seat convicting and four passing produces a PASS. That is majority rule working
    as specified, and it is also exactly how a panel loses a finding that only one seat
    was able to see -- switching JUDGE_COUNCIL_RULE to "any" is what buys that back, at
    the cost of every seat's false positives.
    """
    council = _council({"a": (0, 0), "b": (1, 1), "c": (1, 1), "d": (1, 1), "e": (1, 1)})
    result = await council.judge(system_prompt="p", payload={})
    assert result.passed is True
    assert result.rubric.correctness == 1

    strict = _council({"a": (0, 0), "b": (1, 1), "c": (1, 1), "d": (1, 1), "e": (1, 1)}, rule="any")
    assert (await strict.judge(system_prompt="p", payload={})).passed is False


@pytest.mark.anyio
async def test_a_seat_that_raises_abstains_and_does_not_take_the_panel_down():
    judges = {"a": _FakeJudge("a", 0, 0), "b": _ExplodingJudge(), "c": _FakeJudge("c", 0, 0)}
    council = CouncilJudge(("a", "b", "c"), judge_for=lambda m: judges[m])
    result = await council.judge(system_prompt="p", payload={})
    assert result.rubric.correctness == 0
    ballot = json.loads(result.raw_response)["ballot"]
    dead = next(seat for seat in ballot if seat["model"] == "b")
    assert dead["abstained"] is True
    assert "provider is down" in dead["error"]


@pytest.mark.anyio
async def test_abstentions_never_count_as_agreement():
    """Two silent seats must not turn a single conviction into a minority."""
    council = _council({"a": (0, 0), "b": (None, None), "c": (None, None)})
    result = await council.judge(system_prompt="p", payload={})
    assert result.rubric.correctness == 0
    assert result.passed is False


@pytest.mark.anyio
async def test_total_abstention_reports_no_rubric_rather_than_a_pass():
    """No quorum must reach L5 as JUDGE_UNPARSEABLE (a WARN), not as an acquittal."""
    council = _council({"a": (None, None), "b": (None, None)})
    result = await council.judge(system_prompt="p", payload={})
    assert result.rubric is None
    assert result.parse_path == "council_no_quorum"


@pytest.mark.anyio
async def test_ballot_records_every_seat_for_audit():
    council = _council({"a": (0, 0), "b": (1, 1), "c": (1, 1)})
    result = await council.judge(system_prompt="p", payload={})
    record = json.loads(result.raw_response)
    assert record["council"]["rule"] == "majority"
    assert [s["model"] for s in record["ballot"]] == ["a", "b", "c"]
    assert record["combined"] == {"correctness": 1, "material_completeness": 1}
    assert result.cost_usd == pytest.approx(0.03)


@pytest.mark.anyio
async def test_reasons_quote_convicting_seats_and_name_the_dissent():
    council = _council({"a": (0, 0), "b": (0, 0), "c": (1, 1)})
    result = await council.judge(system_prompt="p", payload={})
    joined = " ".join(result.reasons)
    assert "[a]" in joined and "[b]" in joined
    assert "dissenting" in joined and "c" in joined


def test_bad_construction_fails_loudly():
    """A misconfigured panel must raise, not silently degrade to something plausible.

    A council is selected by config, so a typo'd rule would otherwise become a working
    panel applying a rule nobody chose.
    """
    with pytest.raises(ValueError, match="unknown council rule"):
        CouncilJudge(("a", "b"), rule="plurality")
    with pytest.raises(ValueError, match="at least one seat"):
        CouncilJudge(())


# --- the parse fix ----------------------------------------------------------------


def test_binary_verdict_json_is_accepted():
    """The shape claude-sonnet-5 actually returned, which used to be discarded.

    It reached the JSON ladder, parsed cleanly, and was then rejected for lacking the
    legacy 0-4 ``rubric`` object -- so a conviction was recorded as a pass.
    """
    passed, rubric, reasons = validate_judge_object(
        {
            "correctness": 0,
            "material_completeness": 0,
            "defects": [{"type": "Wrong rule", "explanation": "states the opposite"}],
        }
    )
    assert passed is False
    assert (rubric.correctness, rubric.material_completeness) == (0, 0)
    assert "Wrong rule" in reasons[0]


def test_binary_verdict_nested_under_rubric_is_accepted():
    passed, rubric, _ = validate_judge_object(
        {"rubric": {"correctness": 1, "material_completeness": 1}}
    )
    assert passed is True
    assert rubric.correctness == 1


def test_binary_verdict_missing_a_dimension_is_rejected():
    """Half a verdict is not a verdict: guessing the other half invents a score."""
    with pytest.raises(JudgeValidationError, match="material_completeness"):
        validate_judge_object({"correctness": 1})


def test_binary_verdict_out_of_range_is_rejected():
    with pytest.raises(JudgeValidationError, match="must be 0 or 1"):
        validate_judge_object({"correctness": 3, "material_completeness": 1})


def test_legacy_rubric_still_validates():
    passed, rubric, _ = validate_judge_object(
        {
            "passed": True,
            "rubric": {
                "factual_faithfulness": 4,
                "contextual_accuracy": 3,
                "citation_integrity": 4,
                "responsiveness": 4,
            },
            "reasons": ["fine"],
        }
    )
    assert passed is True
    assert rubric.factual_faithfulness == 4
    assert rubric.correctness is None
