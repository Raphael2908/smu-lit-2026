"""The fail-fast gate."""

from __future__ import annotations

from verifier.contracts.enums import Verdict
from verifier.contracts.runs import RunOptions
from verifier.pipeline import gate


def test_a_deterministic_fail_skips_the_judge():
    run, reason = gate.should_run_judge(Verdict.FAIL, RunOptions())
    assert run is False
    assert reason == gate.REASON_HARD_FAIL


def test_a_pass_runs_the_judge_with_no_skip_reason():
    run, reason = gate.should_run_judge(Verdict.PASS, RunOptions())
    assert run is True
    assert reason is None


def test_a_warn_still_runs_the_judge():
    """WARN is not a failure. Only a hard FAIL closes the gate."""
    run, reason = gate.should_run_judge(Verdict.WARN, RunOptions())
    assert run is True
    assert reason is None


def test_client_opt_out_beats_everything():
    run, reason = gate.should_run_judge(Verdict.PASS, RunOptions(skip_judge=True))
    assert run is False
    assert reason == gate.REASON_CLIENT_OPT_OUT
    # Even combined with force_judge, opting out wins: it is the client's call.
    run, reason = gate.should_run_judge(Verdict.PASS, RunOptions(skip_judge=True, force_judge=True))
    assert run is False


def test_force_judge_overrides_a_deterministic_fail():
    run, reason = gate.should_run_judge(Verdict.FAIL, RunOptions(force_judge=True))
    assert run is True
    assert reason == gate.REASON_FORCED


def test_pending_never_spends_judge_tokens():
    run, reason = gate.should_run_judge(Verdict.PENDING, RunOptions())
    assert run is False
    assert reason == gate.REASON_DETERMINISTIC_INCOMPLETE


def test_short_circuited_distinguishes_the_invariant_from_a_client_choice():
    assert gate.decide(Verdict.FAIL, RunOptions()).short_circuited is True
    assert gate.decide(Verdict.PENDING, RunOptions()).short_circuited is True
    # Opting out is a skip, not the invariant firing.
    assert gate.decide(Verdict.PASS, RunOptions(skip_judge=True)).short_circuited is False
    assert gate.decide(Verdict.PASS, RunOptions()).short_circuited is False
