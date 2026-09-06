"""Verdict aggregation -- the one place a verdict is ever computed.

THE INVARIANT: **the judge can convict but never acquit.**

Layers 1-3 are deterministic. If any of them produces a FAIL finding, the run has
failed on machine-checkable grounds and no amount of model opinion may undo it. L3's
only channel into the verdict is *adding* findings; ``JudgeResult`` has no field
capable of clearing one, and if a future schema grew such a field this module would
ignore it. ``lattice_min`` is monotone, so the final verdict can only move DOWN from
the deterministic one.

That is the answer to "who audits the auditor?", and it is worth stating in code
because it is the only property of this system that a reviewer cannot verify by
reading the UI: everything else is visible, this is structural.

The guard is an exception, not a log line. A silently-laundered verdict is precisely
the failure this project exists to prevent, so it must stop the run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from verifier.contracts.enums import VERDICT_ORDER, Layer, LayerStatus, Severity, Verdict
from verifier.contracts.findings import Finding
from verifier.contracts.layers import LayerResult
from verifier.contracts.runs import RunOptions
from verifier.errors import ContractViolation

__all__ = [
    "FinalVerdict",
    "assert_additive",
    "assert_monotone",
    "deterministic_verdict",
    "finalize",
    "lattice_min",
    "verdict_of_layers",
]


def deterministic_verdict(findings: Iterable[Finding]) -> Verdict:
    """FAIL beats WARN beats PASS. The only rule for turning findings into a verdict.

    Note what is NOT consulted: ``LayerStatus``. A layer that crashed reports
    ``LayerStatus.ERROR`` and contributes a WARN finding (see ``BaseLayer.run``), so an
    ERROR can never fail a run. Failing someone's legal work because our own code broke
    would be the worst false positive this system can produce.
    """
    findings = tuple(findings)
    if any(f.severity is Severity.FAIL for f in findings):
        return Verdict.FAIL
    if any(f.severity is Severity.WARN for f in findings):
        return Verdict.WARN
    return Verdict.PASS


def verdict_of_layers(results: Iterable[LayerResult]) -> Verdict:
    """Verdict over a set of layer results, via their findings."""
    return deterministic_verdict(f for r in results for f in r.findings)


def lattice_min(a: Verdict, b: Verdict) -> Verdict:
    """Meet on the FAIL < WARN < PASS lattice. Monotone by construction.

    PENDING is not in ``VERDICT_ORDER`` -- it is a lifecycle state, not a verdict, and
    comparing it would silently mask a bug. Treat it as "nothing known yet": the other
    operand wins.
    """
    if a is Verdict.PENDING:
        return b
    if b is Verdict.PENDING:
        return a
    return a if VERDICT_ORDER[a] <= VERDICT_ORDER[b] else b


def assert_monotone(final: Verdict, deterministic: Verdict) -> None:
    """The tripwire. Raises rather than logs: a laundered verdict must stop the run.

    Kept as a named, exported function so it can be tested directly and so the
    orchestrator can re-assert it after any future post-processing step.
    """
    if deterministic is Verdict.PENDING or final is Verdict.PENDING:
        # Nothing to compare; the deterministic phase has not concluded.
        return
    if VERDICT_ORDER[final] > VERDICT_ORDER[deterministic]:
        raise ContractViolation(
            "The judge cannot acquit: final verdict "
            f"{final.value!r} is more favourable than the deterministic verdict "
            f"{deterministic.value!r}. Aggregation must be monotone downward."
        )


def assert_additive(det_findings: Sequence[Finding], final_findings: Sequence[Finding]) -> None:
    """The judge may only ADD. Every deterministic finding must survive verbatim.

    A verdict that stayed FAIL while the evidence for it vanished from the panel would
    be laundering by another route: the user would see a red cross with no reason.
    """
    surviving = {f.id for f in final_findings}
    missing = [f for f in det_findings if f.id not in surviving]
    if missing:
        raise ContractViolation(
            "Deterministic findings were dropped during finalisation: "
            + ", ".join(f.code.value for f in missing)
        )


@dataclass(frozen=True)
class FinalVerdict:
    verdict: Verdict
    findings: tuple[Finding, ...] = ()
    judge_ran: bool = False
    #: What the judge concluded on its own, before the meet with the deterministic
    #: verdict. Recorded for the panel so a user can see the judge's opinion *and* see
    #: that it did not override anything.
    judge_verdict: Verdict = Verdict.PENDING
    detail: dict[str, object] = field(default_factory=dict)


def finalize(
    det_verdict: Verdict,
    det_findings: Sequence[Finding],
    judge_result: LayerResult | None,
) -> FinalVerdict:
    """Combine the deterministic verdict with the judge's, if the judge ran at all.

    The judge may only ADD findings. Its schema has no field capable of clearing one,
    and any such field would be ignored here: the deterministic findings are carried
    through verbatim and the judge's are appended. ``lattice_min`` is monotone, so the
    verdict can only move DOWN.
    """
    det_findings = tuple(det_findings)

    if judge_result is None:
        final = det_verdict
        judge_verdict = Verdict.PENDING
        findings = det_findings
    else:
        judge_findings = tuple(judge_result.findings)
        # Deterministic findings FIRST and unmodified -- the judge appends, never
        # replaces. If this ever became `judge_findings + det_findings` filtered, the
        # guard below is what would catch it.
        findings = det_findings + judge_findings
        judge_verdict = deterministic_verdict(judge_findings)
        final = lattice_min(det_verdict, judge_verdict)

    # Belt and braces on top of lattice_min's monotonicity. This is deliberately a
    # second, independent statement of the invariant: lattice_min could be replaced by
    # a well-meaning refactor, this cannot be satisfied by one.
    assert_monotone(final, det_verdict)

    # And the other half of "may only add": nothing deterministic was dropped.
    if judge_result is not None:
        assert_additive(det_findings, findings)

    return FinalVerdict(
        verdict=final,
        findings=findings,
        judge_ran=judge_result is not None,
        judge_verdict=judge_verdict,
        detail={
            "deterministic_verdict": det_verdict.value,
            "judge_verdict": judge_verdict.value,
            "judge_status": judge_result.status.value if judge_result else None,
        },
    )


def summarise_layers(
    results: Sequence[LayerResult],
) -> dict[Layer, LayerResult]:
    """Index layer results by layer, keeping the last write for a repeated layer.

    Every layer reports exactly once. Source trust used to run twice under its own
    layer key and let the second result supersede the first; it is now L1's sub-check 1c,
    and L1's pre-fetch pass is discarded unless it failed.
    """
    return {r.layer: r for r in results}


def has_hard_failure(results: Iterable[LayerResult]) -> bool:
    """True if any layer produced a FAIL finding. ERROR status is explicitly not a
    failure -- see ``deterministic_verdict``."""
    return any(r.has_fail for r in results)


def layer_status_is_fatal(status: LayerStatus) -> bool:
    """Only an explicit FAIL is fatal. ERROR, SKIPPED and NOT_APPLICABLE are not."""
    return status is LayerStatus.FAIL


def options_or_default(options: RunOptions | None) -> RunOptions:
    return options if options is not None else RunOptions()
