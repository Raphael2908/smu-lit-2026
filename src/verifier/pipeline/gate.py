"""The fail-fast gate: whether L4 is consulted at all.

WHY FAIL-FAST. Three reasons, in order of importance:

1. **Integrity.** If the judge never sees a run that already failed deterministically,
   it cannot possibly launder it. The invariant in ``aggregate.finalize`` makes
   laundering harmless; this gate makes it impossible. Defence in depth on the one
   property the product is sold on.
2. **Honesty of the artifact.** A verdict of "the citation does not exist" needs no
   second opinion, and inviting one implies the deterministic finding was negotiable.
3. **Cost and latency.** A hard failure spends zero judge tokens and returns in the
   deterministic budget rather than waiting on a frontier model.

``force_judge`` exists only as a debug/eval escape hatch -- it lets a researcher see
what the judge *would* have said about a failing output. It changes what runs, never
what the verdict is: ``finalize`` still refuses to let the judge acquit.
"""

from __future__ import annotations

from dataclasses import dataclass

from verifier.contracts.enums import Verdict
from verifier.contracts.runs import RunOptions

__all__ = [
    "REASON_CLIENT_OPT_OUT",
    "REASON_DETERMINISTIC_INCOMPLETE",
    "REASON_FORCED",
    "REASON_HARD_FAIL",
    "GateDecision",
    "should_run_judge",
]

REASON_CLIENT_OPT_OUT = "client_opt_out"
REASON_FORCED = "forced"
REASON_HARD_FAIL = "hard_deterministic_fail"
REASON_DETERMINISTIC_INCOMPLETE = "deterministic_incomplete"


@dataclass(frozen=True)
class GateDecision:
    run_judge: bool
    reason: str | None = None

    @property
    def short_circuited(self) -> bool:
        """True when a deterministic outcome meant the judge was never consulted.

        Opting out via ``skip_judge`` is a client choice, not a short circuit, so it is
        reported as a skip but not as the invariant firing.
        """
        return not self.run_judge and self.reason in (
            REASON_HARD_FAIL,
            REASON_DETERMINISTIC_INCOMPLETE,
        )


def should_run_judge(det_verdict: Verdict, options: RunOptions) -> tuple[bool, str | None]:
    """Return ``(run_judge, reason)``.

    The reason is populated on every skip -- the panel renders it verbatim, because
    "we did not ask the model, and here is why" is the invariant made legible.
    """
    if options.skip_judge:
        return False, REASON_CLIENT_OPT_OUT
    if options.force_judge:
        # Debug/eval only. The judge runs, but aggregate.finalize still refuses to let
        # it improve the verdict.
        return True, REASON_FORCED
    if det_verdict is Verdict.PENDING:
        # The deterministic phase did not conclude. Do not spend judge tokens on an
        # incomplete picture, and do not pretend the run passed.
        return False, REASON_DETERMINISTIC_INCOMPLETE
    # FAIL-FAST: any deterministic failure skips the judge entirely.
    if det_verdict is Verdict.FAIL:
        return False, REASON_HARD_FAIL
    return True, None


def decide(det_verdict: Verdict, options: RunOptions) -> GateDecision:
    run_judge, reason = should_run_judge(det_verdict, options)
    return GateDecision(run_judge=run_judge, reason=reason)
