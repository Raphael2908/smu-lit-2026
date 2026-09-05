"""L5 as a panel: several judges polled independently, their votes combined.

WHY A COUNCIL AT ALL. Layers 1-4 are deterministic and can be tested. L5 reasons, and
therefore is the one layer whose failure no test in this repo can catch. A single model
there is a single point of failure with no second opinion anywhere in the system.

WHAT INDEPENDENCE MEANS HERE, AND WHY IT IS LOAD-BEARING. Each seat is a separate
request carrying only the system prompt and the same frozen ``JudgeContext``. No seat is
told the others exist, none sees another's verdict, and nothing is combined until every
seat has answered. Seats are issued concurrently for wall-clock only -- concurrency
shares no state between them. If seats could see each other the panel would collapse
toward whichever spoke first, and five correlated votes look like agreement while
carrying the information of one.

WHAT THE MEASUREMENTS ACTUALLY SHOWED, INCLUDING THE PART THAT ARGUES AGAINST THIS
MODULE. Run ``scripts/council_probe.py`` for the full sweep. In short:

* On SUBSTANTIVE CORRECTNESS a panel helps in exactly one way: it cancels a single
  seat's sampling noise. One seat scored a correct answer wrong in 1 draw of 7; three
  seats under majority never did.
* On MATERIAL COMPLETENESS majority rule OUTVOTED a lone dissenting seat. Whether that
  seat was right was never settled -- the disagreement was substantive, not noise, and
  no independent ground truth was available. So the panel's effect there is unproven in
  both directions, and it is not evidence that more seats are more accurate.
* Polling ONE model five times changed nothing at all. A systematic disagreement does
  not average out, so only vendor diversity moves a verdict. That is why the roster is
  five different flagships rather than five samples of one.
* Model RECENCY mattered more than seat COUNT. On one live pair the previous generation
  of this roster split 1-4 on correctness; the current generation went 5-0 on the same
  answer with the same context.

None of that makes a panel free. It multiplies cost and latency by the number of seats,
and it multiplies the surface for a judge to hallucinate an authority of its own -- which
two runs observed, and which nothing in this pipeline checks, because L1 verifies
citations in the ANSWER and never in the judge's own reasoning.

THE INVARIANT IS UNCHANGED. A council can convict but never acquit: whatever this
returns is still intersected with the deterministic verdict by ``pipeline.aggregate``,
which raises rather than let any judge improve a verdict.
"""

from __future__ import annotations

import asyncio
import json
import time

from verifier.providers.base import Judge, JudgeResult, JudgeRubric

__all__ = ["CouncilJudge", "Seat", "combine", "council_from_settings"]

#: Dimensions the panel votes on. A seat that did not score one simply does not vote on
#: it; unscored is not zero, and inventing a vote is the one thing a judge layer must
#: never do.
DIMENSIONS = ("correctness", "material_completeness")

RULES = ("majority", "any", "unanimous")


class Seat:
    """One judge and the verdict it returned. ``result`` is None when the seat failed."""

    __slots__ = ("model", "result", "error", "latency_ms")

    def __init__(
        self,
        model: str,
        result: JudgeResult | None,
        error: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        self.model = model
        self.result = result
        self.error = error
        self.latency_ms = latency_ms

    def vote(self, dimension: str) -> int | None:
        """This seat's score on one dimension, or None if it did not cast one."""
        if self.result is None or self.result.rubric is None:
            return None
        return getattr(self.result.rubric, dimension, None)

    @property
    def abstained(self) -> bool:
        return self.result is None or self.result.rubric is None


def combine(votes: list[int | None], rule: str) -> int | None:
    """Turn one dimension's votes into one score. None means nobody voted.

    ABSTENTIONS DO NOT VOTE. A seat that timed out, errored, or returned something
    unreadable has not formed an opinion, and counting its silence as agreement would
    let an outage quietly acquit an answer.
    """
    cast = [v for v in votes if v is not None]
    if not cast:
        return None
    convicting = sum(1 for v in cast if v == 0)
    if rule == "any":
        return 0 if convicting >= 1 else 1
    if rule == "unanimous":
        return 0 if convicting == len(cast) else 1
    return 0 if convicting * 2 > len(cast) else 1


class CouncilJudge:
    """``Judge`` over several models. Implements the same one-method protocol as one."""

    provider = "council"

    def __init__(
        self,
        seats: tuple[str, ...] | list[str],
        *,
        rule: str = "majority",
        concurrency: int = 5,
        judge_for=None,  # noqa: ANN001 - injected in tests; a str -> Judge factory
    ) -> None:
        if not seats:
            raise ValueError("a council needs at least one seat")
        if rule not in RULES:
            raise ValueError(f"unknown council rule {rule!r}; expected one of {RULES}")
        self.seats = tuple(seats)
        self.rule = rule
        self.concurrency = max(1, concurrency)
        self._judge_for = judge_for or _default_judge_for
        self.model = f"council[{rule}]({','.join(self.seats)})"

    async def judge(self, *, system_prompt: str, payload: dict) -> JudgeResult:
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def poll(model: str) -> Seat:
            seat_started = time.perf_counter()
            async with semaphore:
                try:
                    judge = self._judge_for(model)
                    result = await judge.judge(system_prompt=system_prompt, payload=payload)
                except Exception as exc:  # noqa: BLE001 - a dead seat abstains, never raises
                    return Seat(
                        model,
                        None,
                        error=f"{type(exc).__name__}: {exc}",
                        latency_ms=int((time.perf_counter() - seat_started) * 1000),
                    )
            return Seat(model, result, latency_ms=int((time.perf_counter() - seat_started) * 1000))

        polled = await asyncio.gather(*(poll(m) for m in self.seats))
        return self._tally(list(polled), int((time.perf_counter() - started) * 1000))

    def _tally(self, seats: list[Seat], elapsed_ms: int) -> JudgeResult:
        cost = sum(s.result.cost_usd for s in seats if s.result is not None)
        scores: dict[str, int] = {}
        for dimension in DIMENSIONS:
            combined = combine([s.vote(dimension) for s in seats], self.rule)
            if combined is not None:
                scores[dimension] = combined

        record = self._record(seats, scores)

        if not scores:
            # Every seat abstained. Report NO rubric, which L5 turns into
            # JUDGE_UNPARSEABLE at WARN -- a panel we could not read has convicted
            # nobody, and must not be allowed to acquit anyone either.
            return JudgeResult(
                passed=True,
                rubric=None,
                reasons=[],
                raw_response=record,
                parse_path="council_no_quorum",
                latency_ms=elapsed_ms,
                cost_usd=cost,
                model=self.model,
                provider=self.provider,
            )

        return JudgeResult(
            passed=all(v == 1 for v in scores.values()),
            rubric=JudgeRubric(**scores),
            reasons=_attributed_reasons(seats, scores),
            raw_response=record,
            parse_path=f"council_{self.rule}",
            retries=sum(s.result.retries for s in seats if s.result is not None),
            latency_ms=elapsed_ms,
            cost_usd=cost,
            model=self.model,
            provider=self.provider,
        )

    def _record(self, seats: list[Seat], scores: dict[str, int]) -> str:
        """The per-seat ballot, as JSON, carried on ``raw_response``.

        A panel verdict with the votes thrown away is unauditable: a reader cannot tell
        4-1 from 5-0, and those mean very different things about how much to trust it.
        ``JudgeResult`` is a frozen contract with no field for this, so it rides here.
        """
        return json.dumps(
            {
                "council": {"rule": self.rule, "seats": list(self.seats)},
                "combined": scores,
                "ballot": [
                    {
                        "model": s.model,
                        "correctness": s.vote("correctness"),
                        "material_completeness": s.vote("material_completeness"),
                        "abstained": s.abstained,
                        "parse_path": None if s.result is None else s.result.parse_path,
                        "latency_ms": s.latency_ms,
                        "cost_usd": None if s.result is None else s.result.cost_usd,
                        "error": s.error,
                        "reasons": [] if s.result is None else list(s.result.reasons)[:6],
                    }
                    for s in seats
                ],
            },
            indent=2,
        )


def _attributed_reasons(seats: list[Seat], scores: dict[str, int]) -> list[str]:
    """Reasons from the seats, each tagged with the model that gave it.

    Only CONVICTING seats are quoted, and only on a dimension the panel actually
    failed. Quoting a seat that voted to pass would put an argument for conviction in
    front of a reader next to a verdict that did not convict.
    """
    convicted = {d for d, v in scores.items() if v == 0}
    if not convicted:
        return []
    out: list[str] = []
    for seat in seats:
        if seat.result is None:
            continue
        if not any(seat.vote(d) == 0 for d in convicted):
            continue
        name = seat.model.split("/")[-1]
        out.extend(f"[{name}] {reason}" for reason in seat.result.reasons[:4])
    dissent = [
        s.model.split("/")[-1]
        for s in seats
        if s.result is not None and not s.abstained and s.result.passed
    ]
    if dissent:
        out.append(f"(dissenting: {', '.join(dissent)} would have passed this answer)")
    abstained = [s.model.split("/")[-1] for s in seats if s.abstained]
    if abstained:
        out.append(f"(abstained, no verdict read: {', '.join(abstained)})")
    return out


def _default_judge_for(model: str) -> Judge:
    from verifier.providers.openrouter_llm import OpenRouterJudge

    return OpenRouterJudge(model=model)


def council_from_settings() -> CouncilJudge:
    from verifier.settings import settings

    return CouncilJudge(
        settings.council_models,
        rule=settings.JUDGE_COUNCIL_RULE,
        concurrency=settings.JUDGE_COUNCIL_CONCURRENCY,
    )
