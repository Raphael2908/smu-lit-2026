"""Does a COUNCIL of L5 judges beat one? Measured, on a real run, against ground truth.

    uv run python -m scripts.council_probe                 # the full sweep
    uv run python -m scripts.council_probe --draws 3       # cheaper, noisier
    uv run python -m scripts.council_probe --dry-run       # phase 1 only, no judge spend

L5 is the one layer that reasons, and therefore the one layer that can be wrong in a way
no test catches. The proposal is to replace the single judge with a panel. This script
answers whether that buys anything, at 1, 3 and 5 seats.

WHY TWO ANSWERS, NOT ONE. A council that convicts everything scores 100% at catching a
planted error. Sensitivity alone is not a measurement, so every arm is scored on BOTH:

  ANSWER_FLAWED  ground truth Correctness = 0   -> CATCH RATE        (higher is better)
  ANSWER_CLEAN   ground truth Correctness = 1   -> FALSE ACCUSATION  (lower is better)

Convictions alone only say whether a panel is STRICTER, never whether it is RIGHT, so
phase 4 scores each rubric dimension against ``GROUND_TRUTH`` instead -- see the comment
on that constant for why its completeness value is not what the first run assumed, and
for the finding that came out of being wrong about it.

Both answers cite the SAME real authority and answer the SAME question, so L1-L4 see
near-identical work and the only thing that moves is whether the law is stated correctly.
The flawed one is not subtly wrong: Spandeck [115] holds that a single test applies
"regardless of the nature of the damage caused", and expressly declines to import
Caparo. ANSWER_FLAWED asserts the exact opposite of both. A judge that passes it has
failed at the thing L5 exists to do, and there is no reading of the judgment under which
it is right -- which is what makes it usable as ground truth rather than as an opinion.

WHY THE CONTEXT IS FROZEN. Phase 1 runs the real pipeline once per answer and captures
the JudgeContext the orchestrator actually built -- the resolved citations, the passages
L3 actually retrieved, the deterministic findings. Phase 2 replays that exact context at
every seat. Re-running the whole pipeline per draw would re-fetch, re-embed and re-rank,
so two judges would be scoring subtly different evidence and the comparison would be
measuring retrieval noise as if it were disagreement between judges. It is also ~50x
cheaper, which is what makes enough draws affordable to say anything at all.

WHY DRAWS ARE POOLED, NOT RUN PER ARM. Each model is sampled ``--draws`` times per
answer, and councils are then formed by SUBSAMPLING that pool. A 3-seat council is not a
separate experiment costing 3 more calls; it is three draws already paid for, combined.
This is what lets one bill answer 1, 3 and 5 seats at once, and it is why the arms are
statistically comparable: they are built from the same draws.

THE JUDGE SENDS NO TEMPERATURE (providers/openrouter_llm.py builds the body without
one), so repeated calls to a single model genuinely vary. That is what makes the
homogeneous arm meaningful rather than a constant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verifier.contracts.citations import Resolution
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import Layer, LayerStatus, ResolutionMethod, ResolutionStatus
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.contracts.runs import RunOptions, VerifyRequest
from verifier.layers.l5_judge import FaithfulnessJudgeLayer, JudgeContext
from verifier.pipeline.orchestrator import Orchestrator, new_run_id

# --- the source ---------------------------------------------------------------------

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "corpus"
CORPUS_FILE = "2007_SGCA_37.html"
CORPUS_URL = "https://www.elitigation.sg/gd/s/2007_SGCA_37"


def load_judgment() -> SourceDocument:
    """The real judgment, from the copy the repo already keeps.

    RESOLVED FROM CACHE, NOT FETCHED, AND THAT IS THE POINT. A live eLitigation fetch
    failed while this was being written (status=error -> L3 NOT_APPLICABLE -> zero
    passages), and a judge handed no passages is asked to reason from memory -- which
    the prompt forbids and which would silently turn this into a measurement of the
    models' recall of Singapore law rather than of council size.

    Serving the same parsed judgment to every seat also removes retrieval noise from
    the comparison: two judges disagreeing must be disagreeing about the law, not about
    which paragraphs they happened to be shown.
    """
    html = (CORPUS / CORPUS_FILE).read_text(encoding="utf-8", errors="ignore")
    from verifier.sources.elitigation import ElitigationAdapter

    return ElitigationAdapter().parse(html, CORPUS_URL)


class _CorpusAdapter:
    """Minimal stand-in for the source adapter: the orchestrator asks it for the
    document behind a resolved URL, and that is the whole interface it uses."""

    def __init__(self, document: SourceDocument) -> None:
        self._document = document

    def document_for(self, url: str | None) -> SourceDocument | None:
        return self._document if url == self._document.source_url else None


def corpus_resolver(document: SourceDocument):
    async def resolve_one(citation_key: str) -> Resolution:
        return Resolution(
            citation_key=citation_key,
            status=ResolutionStatus.RESOLVED,
            method=ResolutionMethod.CACHE,
            url=document.source_url,
            domain=document.domain,
            fetch_strategy=document.fetch_strategy,
            # L1 cross-checks the neutral citation against the page TITLE (F4), so this
            # must be the title, not the case name -- passing the case name here makes
            # L1 report RESOLVED_WRONG_DOC against the very document it was handed.
            title=document.neutral_citation,
            case_name=document.case_name,
            confidence=1.0,
            cached=True,
        )

    return resolve_one


# --- the panel ---------------------------------------------------------------------

#: Seat order IS the experiment: the k-seat council is ROSTER[:k]. Seat 1 is the
#: incumbent judge, so the 1-seat arm is exactly what the pipeline does today and every
#: larger arm is measured as a change FROM it. Seats 2 and 3 are different vendors, so
#: the 3-seat arm is three independent training pipelines rather than three samples of
#: one -- if diversity buys anything, it should appear by seat 3.
ROSTER: tuple[str, ...] = (
    "anthropic/claude-opus-5",  # the incumbent (settings.JUDGE_MODEL)
    "openai/gpt-5",
    "google/gemini-2.5-pro",
    "anthropic/claude-sonnet-5",
    "deepseek/deepseek-r1",
)

SIZES: tuple[int, ...] = (1, 3, 5)

# --- the scenario ------------------------------------------------------------------

QUESTION = "What is the test for a duty of care in Singapore?"

#: Ground truth: CORRECT. Lifted verbatim from scripts/l3_probe.DEFAULT_ANSWER, where
#: every proposition is already checked against the numbered paragraphs of the judgment
#: rather than remembered. Borrowing it means the clean arm rests on work that was
#: verified for another purpose, not on this script's author being sure.
ANSWER_CLEAN = (
    "Singapore applies a single two-stage test for the imposition of a duty of care in "
    "negligence, irrespective of the type of damage claimed: Spandeck Engineering (S) "
    "Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37. The test is premised "
    "on proximity and policy considerations, and is preceded by a preliminary "
    "requirement of factual foreseeability. Legal proximity encompasses physical, "
    "circumstantial and causal proximity, as well as the twin criteria of voluntary "
    "assumption of responsibility and reliance. Policy considerations are applied only "
    "at the second stage, once a prima facie duty of care has been established."
)

#: Ground truth: INCORRECT. Same question, same real citation, opposite law. Two planted
#: errors, both of which the judge prompt names explicitly as high-risk failure modes:
#:
#:   1. "the applicable test depends on the nature of the damage" -- Spandeck [115]
#:      holds a single test applies REGARDLESS of the nature of the damage. This is the
#:      prompt's "mischaracterises what a court decided".
#:   2. "expressly adopted Caparo" -- Spandeck declined to. This is the prompt's
#:      section D, FOREIGN-LAW TRANSPLANT, almost to the letter.
#:
#: Note what is NOT wrong with it: the citation is real, correctly formatted, and
#: genuinely about duty of care. L1-L4 have nothing to object to. Catching this is L5's
#: job alone, which is the whole point of pointing the experiment at it.
ANSWER_FLAWED = (
    "Singapore does not apply a single test for the imposition of a duty of care. "
    "Following the Court of Appeal's decision in Spandeck Engineering (S) Pte Ltd v "
    "Defence Science & Technology Agency [2007] SGCA 37, the applicable test depends on "
    "the nature of the damage suffered. Claims for physical damage are governed by a "
    "two-stage inquiry into proximity and policy considerations. Claims for pure "
    "economic loss must in addition satisfy the three-stage test in Caparo Industries "
    "plc v Dickman, requiring reasonable foreseeability, proximity, and that it be "
    "fair, just and reasonable to impose a duty. The Court of Appeal in Spandeck "
    "expressly adopted the Caparo test for claims in pure economic loss, holding that "
    "the more restrictive English approach was necessary to guard against indeterminate "
    "liability."
)

#: Ground truth PER DIMENSION. The correctness values are checked against the judgment
#: text and are not in dispute. THE COMPLETENESS VALUE FOR ``clean`` IS DISPUTED, and
#: the honest reading of this experiment is that it has no ground truth at all.
#:
#: What happened: the first run asserted material_completeness = 1. Opus 5 dissented
#: 7/7, alone against four vendors, arguing that the answer omits the incremental
#: element of the Spandeck test -- [115] "these two stages are to be approached with
#: reference to the facts of decided cases", [73] "this test is to be applied
#: incrementally". Those paragraphs say what Opus said they say, so the value was
#: flipped to 0.
#:
#: THAT WAS A METHODOLOGICAL ERROR AND IT IS LEFT HERE ON PURPOSE. Checking the
#: paragraphs verified Opus's PREMISES, not its CONCLUSION -- whether the omission is
#: MATERIAL, which is the actual question and which the judgment cannot settle. The
#: ground truth was therefore taken from one of the models under test and then used to
#: score that same model. That is circular, and it is the same failure as setting
#: ground truth by consensus, just with a minority of one.
#:
#: The text cuts both ways. [115] says the test "is a two-stage test, comprising of,
#: first, proximity and, second, policy considerations" -- "comprising" enumerates two
#: elements, and the incremental sentence is arguably about how they are APPROACHED.
#: On that reading the four-vendor majority is right and this value should be 1.
#:
#: Treat every completeness number this script prints as measuring DISAGREEMENT, not
#: accuracy. The correctness numbers are the ones that carry weight.
GROUND_TRUTH: dict[str, dict[str, int]] = {
    "clean": {"correctness": 1, "material_completeness": 0},
    "flawed": {"correctness": 0, "material_completeness": 0},
}

ARMS: dict[str, tuple[str, int]] = {
    # name -> (answer text, ground-truth correctness)
    "clean": (ANSWER_CLEAN, GROUND_TRUTH["clean"]["correctness"]),
    "flawed": (ANSWER_FLAWED, GROUND_TRUTH["flawed"]["correctness"]),
}


# --- phase 1: capture a real JudgeContext ------------------------------------------


class _CaptureJudge:
    """Stands in for L5 during phase 1 so the context is built but no judge is paid.

    ``force_judge`` makes the orchestrator construct the judge layer even if a
    deterministic layer failed, which is exactly the escape hatch pipeline/gate.py
    documents for this use -- a researcher seeing what the judge WOULD have said.
    """

    layer = Layer.L5_JUDGE

    async def run(self, data: LayerInput) -> LayerResult:
        return LayerResult(
            layer=Layer.L5_JUDGE,
            status=LayerStatus.NOT_APPLICABLE,
            detail={"reason": "context_capture_only"},
        )


@dataclass
class Captured:
    context: JudgeContext
    deterministic: dict[str, str]
    verdict: str
    citations: tuple[str, ...]
    passages: int
    cost_usd: float


async def capture(answer: str) -> Captured:
    """Run the real pipeline once and keep the context it built."""
    box: dict[str, JudgeContext] = {}

    def factory(ctx: JudgeContext) -> Any:
        box["ctx"] = ctx
        return _CaptureJudge()

    document = load_judgment()
    orch = Orchestrator(judge_factory=factory, resolve_citation=corpus_resolver(document))
    # The orchestrator only sets ``_source_adapter`` on the branch that loads a live
    # adapter, and ``_documents_for`` reads it to hand the judgment to L1 and L3. With
    # an injected resolver that branch never runs, so the document has to be supplied
    # here or every layer downstream of resolution sees a resolved citation with no
    # text behind it.
    orch._source_adapter = _CorpusAdapter(document)  # noqa: SLF001 - probe wiring
    request = VerifyRequest(
        question=QUESTION,
        ai_output=answer,
        options=RunOptions(force_judge=True),
    )
    state = await orch.run(request, run_id=new_run_id())
    ctx = box.get("ctx")
    if ctx is None:  # pragma: no cover - only if the gate stops constructing the judge
        raise RuntimeError("judge_factory was never called; cannot capture a context")
    return Captured(
        context=ctx,
        deterministic={
            layer.value: result.status.value
            for layer, result in sorted(state.layers.items(), key=lambda kv: kv[0].value)
            if layer is not Layer.L5_JUDGE
        },
        verdict=state.verdict.value,
        citations=ctx.citations,
        passages=len(ctx.retrieved_passages),
        cost_usd=float(state.cost_usd or 0.0),
    )


# --- phase 2: draw verdicts --------------------------------------------------------


@dataclass
class Draw:
    arm: str
    model: str
    index: int
    correctness: int | None
    completeness: int | None
    convicts: bool | None  # None == abstained (unreadable verdict)
    parse_path: str
    status: str
    latency_ms: int
    cost_usd: float
    reasons: list[str] = field(default_factory=list)
    error: str | None = None


async def draw_one(arm: str, answer: str, ctx: JudgeContext, model: str, index: int) -> Draw:
    started = time.perf_counter()
    try:
        judge = OpenRouterJudgeFor(model)
        layer = FaithfulnessJudgeLayer(judge, context=ctx)
        result = await layer.run(
            LayerInput(run_id=f"council-{arm}-{index}", question=QUESTION, ai_output=answer)
        )
    except Exception as exc:  # noqa: BLE001 - a dead seat is data, not a crash
        return Draw(
            arm=arm,
            model=model,
            index=index,
            correctness=None,
            completeness=None,
            convicts=None,
            parse_path="error",
            status="EXCEPTION",
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=0.0,
            error=f"{type(exc).__name__}: {exc}",
        )

    rubric = result.detail.get("rubric") or {}
    correctness = rubric.get("correctness")
    completeness = rubric.get("material_completeness")
    # An unreadable verdict ABSTAINS. Counting it as an acquittal would let a broken
    # seat silently vote, which is the same failure the layer avoids by making
    # JUDGE_UNPARSEABLE a WARN rather than a pass.
    convicts: bool | None
    if correctness is None and completeness is None:
        convicts = None
    else:
        convicts = (correctness == 0) or (completeness == 0)
    return Draw(
        arm=arm,
        model=model,
        index=index,
        correctness=correctness,
        completeness=completeness,
        convicts=convicts,
        parse_path=str(result.detail.get("parse_path", "")),
        status=result.status.value,
        latency_ms=result.duration_ms or int((time.perf_counter() - started) * 1000),
        cost_usd=float(result.detail.get("cost_usd", 0.0) or 0.0),
        reasons=[str(r) for r in (result.detail.get("reasons") or [])][:4],
    )


#: Generous on purpose. The provider default is 90s, and the reasoning models on this
#: roster routinely exceed it on a ~10k-token prompt -- a seat that times out abstains,
#: and an abstention caused by our own clock would be recorded as "this model had no
#: opinion" when in fact it was never allowed to finish forming one.
JUDGE_TIMEOUT_S = 300.0


def OpenRouterJudgeFor(model: str):  # noqa: N802 - reads as a constructor at call sites
    from verifier.providers.openrouter_llm import OpenRouterJudge

    return OpenRouterJudge(model=model, timeout=JUDGE_TIMEOUT_S)


async def draw_all(captured: dict[str, Captured], draws: int, concurrency: int) -> list[Draw]:
    sem = asyncio.Semaphore(concurrency)

    async def guarded(arm: str, answer: str, ctx: JudgeContext, model: str, i: int) -> Draw:
        async with sem:
            return await draw_one(arm, answer, ctx, model, i)

    tasks = [
        guarded(arm, ARMS[arm][0], captured[arm].context, model, i)
        for arm in ARMS
        for model in ROSTER
        for i in range(draws)
    ]
    return list(await asyncio.gather(*tasks))


# --- phase 3: form councils --------------------------------------------------------

#: How a council turns k individual votes into one verdict. All three are reported from
#: the SAME draws, because the choice of rule is a policy decision and the data cannot
#: make it -- but it can price it.
RULES = ("majority", "any", "unanimous")


def council_convicts(votes: list[bool | None], rule: str) -> bool:
    """Abstentions do not vote. A seat that returned nothing readable is not evidence."""
    cast = [v for v in votes if v is not None]
    if not cast:
        return False  # nobody could read the answer -> fail open, as L5 does
    guilty = sum(1 for v in cast if v)
    if rule == "any":
        return guilty >= 1
    if rule == "unanimous":
        return guilty == len(cast)
    return guilty * 2 > len(cast)


def dimension_accuracy(
    pool: dict[tuple[str, str], list[Draw]],
    arm: str,
    seats: list[str],
    rule: str,
    dimension: str,
    trials: int,
    rng: random.Random,
) -> float:
    """P(the council's verdict on ONE dimension matches the verified ground truth).

    Scored per dimension rather than on the combined pass/fail, because the two move in
    opposite directions here and a combined score would average that away into a number
    that describes neither.
    """
    per_seat = [pool.get((arm, s), []) for s in seats]
    if any(not seat for seat in per_seat):
        return float("nan")
    truth = GROUND_TRUTH[arm][dimension]
    hits = 0
    for _ in range(trials):
        votes = [
            (d.correctness if dimension == "correctness" else d.completeness)
            for d in (rng.choice(seat) for seat in per_seat)
        ]
        cast = [v for v in votes if v is not None]
        if not cast:
            verdict = 1  # nobody could read it -> fail open, as L5 does
        else:
            fails = sum(1 for v in cast if v == 0)
            if rule == "any":
                verdict = 0 if fails >= 1 else 1
            elif rule == "unanimous":
                verdict = 0 if fails == len(cast) else 1
            else:
                verdict = 0 if fails * 2 > len(cast) else 1
        if verdict == truth:
            hits += 1
    return hits / trials


def bootstrap(
    pool: dict[tuple[str, str], list[Draw]],
    arm: str,
    seats: list[str],
    rule: str,
    trials: int,
    rng: random.Random,
) -> float:
    """P(council convicts) for one arm, by resampling the draws already paid for."""
    per_seat = [pool.get((arm, s), []) for s in seats]
    if any(not seat for seat in per_seat):
        return float("nan")
    hits = 0
    for _ in range(trials):
        votes = [rng.choice(seat).convicts for seat in per_seat]
        if council_convicts(votes, rule):
            hits += 1
    return hits / trials


# --- reporting ---------------------------------------------------------------------


def pct(x: float) -> str:
    return "  n/a" if x != x else f"{x * 100:5.1f}%"


def report(
    captured: dict[str, Captured],
    draws: list[Draw],
    n_draws: int,
    trials: int,
    seed: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    pool: dict[tuple[str, str], list[Draw]] = {}
    for d in draws:
        pool.setdefault((d.arm, d.model), []).append(d)

    out: dict[str, Any] = {
        "question": QUESTION,
        "roster": list(ROSTER),
        "draws_per_model_per_arm": n_draws,
        "bootstrap_trials": trials,
        "seed": seed,
        "phase1": {
            arm: {
                "deterministic": c.deterministic,
                "verdict": c.verdict,
                "citations": list(c.citations),
                "passages": c.passages,
            }
            for arm, c in captured.items()
        },
    }

    print("\n" + "=" * 78)
    print("PHASE 1 -- the real pipeline, once per answer")
    print("=" * 78)
    for arm, c in captured.items():
        gt = ARMS[arm][1]
        print(f"\n  {arm.upper()}  (ground truth: Correctness = {gt})")
        print(f"    deterministic : {c.deterministic}")
        print(f"    verdict       : {c.verdict}")
        print(f"    citations     : {list(c.citations) or '(none resolved)'}")
        print(f"    passages to L5: {c.passages}")

    # --- per-model behaviour, before any council forms ---
    print("\n" + "=" * 78)
    print(f"PHASE 2 -- each seat alone, {n_draws} independent draws per answer")
    print("=" * 78)
    print(f"\n  {'model':32} {'arm':7} {'convict':>8} {'abstain':>8} {'med ms':>7} {'$':>8}")
    print("  " + "-" * 74)
    per_model: dict[str, Any] = {}
    for model in ROSTER:
        for arm in ARMS:
            ds = pool.get((arm, model), [])
            if not ds:
                continue
            cast = [d for d in ds if d.convicts is not None]
            conv = sum(1 for d in cast if d.convicts)
            abst = len(ds) - len(cast)
            med = statistics.median([d.latency_ms for d in ds]) if ds else 0
            cost = sum(d.cost_usd for d in ds)
            rate = conv / len(cast) if cast else float("nan")
            print(
                f"  {model:32} {arm:7} {pct(rate):>8} {abst:>4}/{len(ds):<3} "
                f"{int(med):>7} {cost:>8.4f}"
            )
            per_model[f"{model}|{arm}"] = {
                "convict_rate": None if rate != rate else rate,
                "abstentions": abst,
                "draws": len(ds),
                "median_latency_ms": int(med),
                "cost_usd": round(cost, 6),
            }
    out["per_model"] = per_model

    # --- councils ---
    print("\n" + "=" * 78)
    print("PHASE 3 -- councils, by size")
    print("=" * 78)
    print("\n  CATCH  = P(convicts the FLAWED answer)   higher is better  <- ground truth 0")
    print("  FALSE  = P(convicts the CLEAN answer)    lower  is better  <- ground truth 1")
    print("  NET    = CATCH - FALSE                   the only number that nets out both")

    councils: dict[str, Any] = {}
    for kind, blurb, seats_for in (
        ("heterogeneous", "different vendors", lambda k: list(ROSTER[:k])),
        ("homogeneous", f"all {ROSTER[0]}", lambda k: [ROSTER[0]] * k),
    ):
        print(f"\n  --- {kind} ({blurb}) ---")
        print(f"  {'rule':11} {'seats':>5} {'CATCH':>8} {'FALSE':>8} {'NET':>8}   panel")
        print("  " + "-" * 74)
        for rule in RULES:
            for k in SIZES:
                seats = seats_for(k)
                catch = bootstrap(pool, "flawed", seats, rule, trials, rng)
                false = bootstrap(pool, "clean", seats, rule, trials, rng)
                net = catch - false
                label = ", ".join(s.split("/")[-1] for s in seats) if k <= 3 else f"{k} seats"
                print(f"  {rule:11} {k:>5} {pct(catch):>8} {pct(false):>8} {pct(net):>8}   {label}")
                councils[f"{kind}|{rule}|{k}"] = {
                    "catch": None if catch != catch else catch,
                    "false_accusation": None if false != false else false,
                    "net": None if net != net else net,
                    "seats": seats,
                }
            print()
    out["councils"] = councils

    # --- the measurement that actually answers the question ---
    # Convictions alone cannot say whether a council is BETTER, only whether it is
    # stricter. Scoring each dimension against its verified ground truth can, and it is
    # what separates "the panel disagreed" from "the panel was wrong".
    print("\n" + "=" * 78)
    print("PHASE 4 -- ACCURACY per dimension, against ground truth in the judgment")
    print("=" * 78)
    dims: dict[str, Any] = {}
    for dim, key in (("correctness", "correctness"), ("completeness", "material_completeness")):
        print(f"\n  --- {dim} ---")
        print(f"  {'rule':11} {'seats':>5} {'flawed':>9} {'clean':>9} {'mean':>9}   panel")
        print("  " + "-" * 70)
        for rule in RULES:
            for k in SIZES:
                seats = list(ROSTER[:k])
                scores = {
                    arm: dimension_accuracy(pool, arm, seats, rule, key, trials, rng)
                    for arm in ARMS
                }
                mean = sum(scores.values()) / len(scores)
                label = ", ".join(s.split("/")[-1] for s in seats) if k <= 3 else f"{k} seats"
                print(
                    f"  {rule:11} {k:>5} {scores['flawed'] * 100:8.1f}% "
                    f"{scores['clean'] * 100:8.1f}% {mean * 100:8.1f}%   {label}"
                )
                dims[f"{dim}|{rule}|{k}"] = {**scores, "mean": mean, "seats": seats}
            print()
    out["dimension_accuracy"] = dims
    out["ground_truth"] = GROUND_TRUTH
    out["draws"] = [vars(d) for d in draws]
    total = sum(d.cost_usd for d in draws)
    print(f"  judge spend this run: ${total:.4f} over {len(draws)} calls")
    out["total_cost_usd"] = round(total, 6)
    return out


# --- entry point -------------------------------------------------------------------


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--draws", type=int, default=7, help="draws per model per answer")
    ap.add_argument("--trials", type=int, default=4000, help="bootstrap resamples")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260905)
    ap.add_argument("--dry-run", action="store_true", help="phase 1 only, no judge spend")
    ap.add_argument("--out", type=Path, default=None, help="write the full record as JSON")
    args = ap.parse_args()

    from verifier.settings import settings

    print(f"provider mode : {settings.PROVIDER_MODE}")
    print(f"judge model   : {settings.JUDGE_MODEL} (seat 1)")
    print(f"prompt version: {settings.JUDGE_PROMPT_VERSION}")

    captured: dict[str, Captured] = {}
    for arm, (answer, _gt) in ARMS.items():
        print(f"\ncapturing context for {arm!r} ...")
        captured[arm] = await capture(answer)

    if args.dry_run:
        for arm, c in captured.items():
            print(f"\n{arm}: {c.deterministic} verdict={c.verdict} passages={c.passages}")
            print(f"  citations: {list(c.citations)}")
        return

    planned = len(ARMS) * len(ROSTER) * args.draws
    print(f"\ndrawing {planned} verdicts ({args.draws} per model per answer) ...")
    draws = await draw_all(captured, args.draws, args.concurrency)

    record = report(captured, draws, args.draws, args.trials, args.seed)
    if args.out:
        args.out.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        print(f"\n  full record -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
