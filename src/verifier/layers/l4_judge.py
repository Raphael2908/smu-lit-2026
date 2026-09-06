"""L4 -- factual faithfulness, judged by a reasoning model.

This layer runs ONLY when L1-L3 all passed. By the time it is consulted the cited
cases exist, the quotes are genuine, the output draws on the cited sources and it
addresses the question. What remains is the one question none of those checks can
answer, and the one embeddings provably cannot: **is the answer true to what the law
actually holds?**

That restriction is not a stylistic choice. docs/03-findings.md Part 2, result 4:
embedding methods reach 95.8% coverage at 0% FPR on *synthetic* hallucinations but
**100% FPR on real ones** from an RLHF-aligned model -- those are "semantically
indistinguishable from faithful responses" and "preserve the 'vibe' of the truth while
altering the facts". Only reasoning judges succeeded. So L4 exists precisely, and only,
where a reasoning model is the proven tool.

Two structural rules this layer enforces:

* **The system prompt is owned by the user.** It lives in ``prompts/judge.md``, is read
  from disk at call time, and is never reconstructed in Python. Editing it needs no code
  change; ``settings.JUDGE_PROMPT_VERSION`` carries the provenance.
* **The judge may only add findings.** Nothing here can clear a deterministic finding,
  and ``pipeline.aggregate.finalize`` would refuse it if it tried.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from verifier.contracts.enums import (
    FindingCode,
    FindingSource,
    Layer,
    LayerStatus,
    Severity,
)
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.layers.base import BaseLayer, status_from_findings
from verifier.providers.base import Judge, JudgeResult, JudgeRubric

__all__ = [
    "PROMPT_PATH",
    "FaithfulnessJudgeLayer",
    "JudgeContext",
    "RetrievedPassage",
    "load_prompt",
    "render_prompt",
]

PROMPT_PATH = Path(__file__).parent / "prompts" / "judge.md"

#: Mirrors providers.openrouter_llm.PARSE_PATH_UNPARSEABLE. Kept as a local constant so
#: a layer never has to import a concrete provider module; a test asserts they agree.
PARSE_PATH_UNPARSEABLE = "unparseable"

#: The placeholders the prompt file may use. Anything else in braces is left alone --
#: which matters, because the prompt legitimately contains a literal JSON example
#: (``{"passed": bool, ...}``). ``str.format`` would raise on it; this will not.
PLACEHOLDERS = (
    "question",
    "ai_output",
    "citations",
    "retrieved_passages",
    "deterministic_findings",
    "uncited_propositions",
)

_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")

#: Rubric thresholds, mirroring the pass rule stated in the prompt file itself:
#: "Set passed false if factual_faithfulness <= 2, or any other dimension is 0 or 1."
#: factual_faithfulness is weighted highest because it is the dimension embeddings
#: cannot assess at all.
_FAITHFULNESS_FAIL_AT_OR_BELOW = 2
_FAITHFULNESS_WARN_AT_OR_BELOW = 3
_OTHER_FAIL_AT_OR_BELOW = 1
_OTHER_WARN_AT_OR_BELOW = 2

_RUBRIC_CODES: dict[str, FindingCode] = {
    "factual_faithfulness": FindingCode.JUDGE_FAILED_FAITHFULNESS,
    "contextual_accuracy": FindingCode.JUDGE_FAILED_CONTEXTUAL_ACCURACY,
    "citation_integrity": FindingCode.JUDGE_FAILED_CITATION_INTEGRITY,
    "responsiveness": FindingCode.JUDGE_FAILED_RESPONSIVENESS,
    # Binary dimensions returned by the active prompt.
    "correctness": FindingCode.JUDGE_FAILED_FAITHFULNESS,
    "material_completeness": FindingCode.JUDGE_FAILED_COMPLETENESS,
}

#: Keep the prompt bounded. A judgment is ~84k chars (F9); a handful of retrieved
#: paragraphs is what makes the faithfulness call checkable by a human reading the
#: panel, and keeps the request small enough to be worth caching.
#:
#: Both budgets live in settings and are read AT CALL TIME, not bound at import.
#: They used to be module constants here duplicating a second pair in
#: l2_alignment.py, and two caps in two files is how evidence gets silently truncated
#: between the layer that retrieves it and the layer that reads it.


@dataclass(frozen=True)
class RetrievedPassage:
    """One passage L2 actually retrieved, with enough provenance to be checkable."""

    text: str
    citation: str | None = None
    paragraph: int | None = None
    score: float | None = None
    source_url: str | None = None
    #: Last paragraph of the passage, when it spans several. Present because a chunk is
    #: a MERGE of paragraphs: labelling one "at [187]" when it also contains [188]-[190]
    #: invites the judge to attribute a proposition to the wrong paragraph, and the
    #: whole purpose of passing provenance is that a human can check it.
    paragraph_to: int | None = None

    def render(self) -> str:
        from verifier.settings import settings

        head = self.citation or "source"
        if self.paragraph is not None:
            span = (
                f"[{self.paragraph}]"
                if self.paragraph_to is None or self.paragraph_to == self.paragraph
                else f"[{self.paragraph}]-[{self.paragraph_to}]"
            )
            head = f"{head} at {span}"
        if self.source_url:
            head = f"{head} <{self.source_url}>"
        body = self.text.strip()
        # A backstop, not the mechanism. L2 now splits an over-long chunk into its own
        # paragraphs and ranks them, so what the judge loses is chosen by relevance
        # rather than by byte offset; this only catches a passage that arrived from
        # somewhere else already too long.
        budget = settings.JUDGE_PASSAGE_MAX_CHARS
        if len(body) > budget:
            body = body[:budget].rstrip() + " ..."
        return f"{head}\n{body}"


@dataclass(frozen=True)
class JudgeContext:
    """Everything L4 needs that ``LayerInput`` has no field for.

    ``LayerInput`` is a frozen contract with no slot for L2's retrieved passages or for
    the deterministic findings, so the orchestrator hands them to the layer at
    construction instead. The layer stays pure with respect to its inputs either way:
    it reads only what it was given, never the DB and never another layer's state.
    """

    citations: tuple[str, ...] = ()
    retrieved_passages: tuple[RetrievedPassage, ...] = ()
    deterministic_findings: tuple[Finding, ...] = ()
    #: Assertions L1a found no authority for. Handed to the judge separately from the
    #: findings list because attribution in prose is exactly the judgement L1a refuses
    #: to make deterministically: whether a citation two sentences away really does
    #: support this claim is a reasoning question, and the judge is where reasoning is
    #: allowed to reach a verdict. It can only ever convict on them.
    uncited_propositions: tuple[str, ...] = ()
    prompt_version: str = ""

    @classmethod
    def empty(cls) -> JudgeContext:
        return cls()


def load_prompt(path: Path | None = None) -> str:
    """Read the user-owned system prompt from disk, at call time.

    Deliberately not cached: editing ``prompts/judge.md`` takes effect on the next run
    with no restart and no code change. Reading a small file per judge call is free
    next to a frontier-model round trip.
    """
    target = path or PROMPT_PATH
    return target.read_text(encoding="utf-8")


def render_prompt(template: str, values: dict[str, str]) -> str:
    """Substitute the known placeholders. Missing ones become a neutral marker.

    A missing placeholder must never crash the run: the prompt is user-owned and may
    legitimately omit any of them, or use one we did not populate for this run.
    """

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key)
        if value is None or not str(value).strip():
            return "(none)"
        return str(value)

    return _PLACEHOLDER_RE.sub(_sub, template)


def _render_findings(findings: Sequence[Finding]) -> str:
    if not findings:
        return "All deterministic checks passed with no findings."
    lines = []
    for f in findings:
        lines.append(f"- [{f.layer.value}/{f.severity.value}] {f.code.value}: {f.message}")
    return "\n".join(lines)


def _render_passages(passages: Sequence[RetrievedPassage]) -> str:
    from verifier.settings import settings

    if not passages:
        return "(no passages were retrieved for this answer)"
    return "\n\n".join(p.render() for p in passages[: settings.MAX_JUDGE_PASSAGES])


def _render_citations(citations: Sequence[str]) -> str:
    if not citations:
        return "(the answer cited no authorities)"
    return "\n".join(f"- {c}" for c in citations)


class FaithfulnessJudgeLayer(BaseLayer):
    """L4. Default-constructible so ``registry.build_layer`` keeps working; the
    orchestrator supplies a bound ``JudgeContext`` and an explicit provider."""

    layer = Layer.L4_JUDGE

    def __init__(
        self,
        judge: Judge | None = None,
        *,
        context: JudgeContext | None = None,
        prompt_path: Path | None = None,
    ) -> None:
        self._judge = judge
        self.context = context or JudgeContext.empty()
        self.prompt_path = prompt_path or PROMPT_PATH

    def _resolve_judge(self) -> Judge:
        if self._judge is not None:
            return self._judge
        from verifier.providers.factory import get_judge

        return get_judge()

    def build_prompt(self, data: LayerInput) -> tuple[str, dict[str, str]]:
        values = {
            "question": data.question,
            "ai_output": data.ai_output,
            "citations": _render_citations(self.context.citations),
            "retrieved_passages": _render_passages(self.context.retrieved_passages),
            "deterministic_findings": _render_findings(self.context.deterministic_findings),
            "uncited_propositions": _render_uncited(self.context.uncited_propositions),
        }
        return render_prompt(load_prompt(self.prompt_path), values), values

    async def _run(self, data: LayerInput) -> LayerResult:
        from verifier.settings import settings

        judge = self._resolve_judge()
        system_prompt, values = self.build_prompt(data)
        payload = {
            **values,
            "run_id": data.run_id,
            "prompt_version": self.context.prompt_version or settings.JUDGE_PROMPT_VERSION,
        }

        started = time.perf_counter()
        try:
            result = await judge.judge(system_prompt=system_prompt, payload=payload)
        except Exception as exc:  # noqa: BLE001 - a provider outage is not a verdict
            # Cannot verify is never guilty: the judge failing to answer must annotate,
            # never fail. Severity.WARN keeps a PASS from becoming a FAIL.
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.ERROR,
                findings=(
                    Finding(
                        id=f"{data.run_id}:L4:judge_error",
                        layer=self.layer,
                        code=FindingCode.JUDGE_ERROR,
                        severity=Severity.WARN,
                        message=f"The faithfulness judge could not be reached: {exc}",
                        source=FindingSource.LLM,
                    ),
                ),
                detail={"error": str(exc)},
            )

        elapsed_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
        findings = tuple(self.findings_from(result, run_id=data.run_id))
        status = status_from_findings(findings)
        if result.rubric is None:
            # Nothing was scored, so there is nothing to call a PASS.
            status = LayerStatus.ERROR if not findings else status

        return LayerResult(
            layer=self.layer,
            status=status,
            findings=findings,
            score=_rubric_score(result.rubric),
            duration_ms=elapsed_ms,
            detail={
                "passed": result.passed,
                "parse_path": result.parse_path,
                "retries": result.retries,
                "model": result.model,
                "provider": result.provider,
                "cost_usd": result.cost_usd,
                "prompt_version": payload["prompt_version"],
                "rubric": _rubric_dict(result.rubric),
                "reasons": list(result.reasons),
                "passages_supplied": len(self.context.retrieved_passages),
            },
        )

    def findings_from(self, result: JudgeResult, *, run_id: str) -> list[Finding]:
        """Map a judge result onto findings. Every one is ``FindingSource.LLM``.

        The UI renders LLM findings differently from deterministic ones, and that
        visual separation is the 'who audits the auditor' answer expressed in pixels.
        """
        reasons = " ".join(r.strip() for r in result.reasons if r and r.strip())

        if result.rubric is None:
            # Unparseable, or a provider that scored nothing. WARN, and only WARN: a
            # judge we could not read is not evidence that the answer is wrong.
            return [
                Finding(
                    id=f"{run_id}:L4:unparseable",
                    layer=self.layer,
                    code=FindingCode.JUDGE_UNPARSEABLE,
                    severity=Severity.WARN,
                    message=(
                        "The faithfulness judge's response could not be parsed, so this "
                        "answer was not assessed for faithfulness."
                    ),
                    source=FindingSource.LLM,
                    evidence=Evidence(
                        extra={
                            "parse_path": result.parse_path,
                            "retries": result.retries,
                            "raw_response": result.raw_response[:2000],
                        }
                    ),
                )
            ]

        findings: list[Finding] = []
        for dimension, code in _RUBRIC_CODES.items():
            score = getattr(result.rubric, dimension, None)
            # A judge populates either the binary pair or the legacy 0-4 set, so the
            # other set is None. Unscored is NOT zero: treating it as a failure would
            # invent findings the judge never made -- which is the one thing this
            # layer must never do.
            if score is None:
                continue
            severity = _severity_for(dimension, score)
            if severity is None:
                continue
            findings.append(
                Finding(
                    id=f"{run_id}:L4:{dimension}",
                    layer=self.layer,
                    code=code,
                    severity=severity,
                    message=_message_for(dimension, score, reasons),
                    source=FindingSource.LLM,
                    evidence=Evidence(
                        score=float(score),
                        threshold=float(_fail_threshold(dimension)),
                        extra={
                            "dimension": dimension,
                            "reasons": list(result.reasons),
                            "model": result.model,
                        },
                    ),
                )
            )

        if not findings and not result.passed:
            # The judge said it failed but no dimension crossed a threshold. Record the
            # disagreement as a WARN rather than inventing a FAIL: prefer a false green
            # to a false red.
            findings.append(
                Finding(
                    id=f"{run_id}:L4:unlocalised",
                    layer=self.layer,
                    code=FindingCode.JUDGE_FAILED_FAITHFULNESS,
                    severity=Severity.WARN,
                    message=(
                        "The faithfulness judge did not pass this answer, but no rubric "
                        f"dimension scored low enough to fail it. {reasons}".strip()
                    ),
                    source=FindingSource.LLM,
                    evidence=Evidence(extra={"reasons": list(result.reasons)}),
                )
            )
        return findings


def _fail_threshold(dimension: str) -> int:
    if dimension in _DIMENSION_MAX:
        return 0
    if dimension == "factual_faithfulness":
        return _FAITHFULNESS_FAIL_AT_OR_BELOW
    return _OTHER_FAIL_AT_OR_BELOW


def _severity_for(dimension: str, score: int) -> Severity | None:
    # Binary dimensions have no middle ground: the prompt defines 0 as "a competent
    # lawyer could be materially misled", which is a failure, not a caution. Inventing
    # a WARN band here would soften a verdict the prompt states categorically.
    if dimension in _DIMENSION_MAX:
        return Severity.FAIL if score == 0 else None
    if dimension == "factual_faithfulness":
        if score <= _FAITHFULNESS_FAIL_AT_OR_BELOW:
            return Severity.FAIL
        if score <= _FAITHFULNESS_WARN_AT_OR_BELOW:
            return Severity.WARN
        return None
    if score <= _OTHER_FAIL_AT_OR_BELOW:
        return Severity.FAIL
    if score <= _OTHER_WARN_AT_OR_BELOW:
        return Severity.WARN
    return None


_DIMENSION_COPY = {
    "factual_faithfulness": "states or implies something the cited sources do not support",
    "contextual_accuracy": "uses a case for something other than what it actually decides",
    "citation_integrity": "attributes a proposition to an authority that does not carry it",
    "responsiveness": "does not answer the question that was asked",
    "correctness": "contains a material legal or factual error",
    "material_completeness": (
        "omits something a competent lawyer would need to answer the question"
    ),
}


def _message_for(dimension: str, score: int, reasons: str) -> str:
    ceiling = _DIMENSION_MAX.get(dimension, 4)
    base = f"The answer {_DIMENSION_COPY[dimension]} (scored {score}/{ceiling})."
    return f"{base} {reasons}".strip()


def _rubric_dict(rubric: JudgeRubric | None) -> dict[str, int] | None:
    """Only the dimensions this judge actually scored.

    A judge populates either the binary pair or the legacy 0-4 set. ``None`` means
    "not assessed", which must not be reported as a zero -- an unscored dimension is
    not a failed one.
    """
    if rubric is None:
        return None
    scored = {
        "factual_faithfulness": rubric.factual_faithfulness,
        "contextual_accuracy": rubric.contextual_accuracy,
        "citation_integrity": rubric.citation_integrity,
        "responsiveness": rubric.responsiveness,
        "correctness": rubric.correctness,
        "material_completeness": rubric.material_completeness,
    }
    return {k: v for k, v in scored.items() if v is not None}


#: Maximum value each dimension can take, so a mean is computed against the right
#: scale. The active prompt's dimensions are binary; the legacy rubric is 0-4.
_DIMENSION_MAX = {"correctness": 1, "material_completeness": 1}


def _rubric_score(rubric: JudgeRubric | None) -> float | None:
    """Mean rubric score, 0-1. Reporting only -- the verdict comes from findings."""
    if rubric is None:
        return None
    values = _rubric_dict(rubric)
    if not values:
        return None
    return sum(v / _DIMENSION_MAX.get(k, 4) for k, v in values.items()) / len(values)


@dataclass
class _PassageHarvest:
    passages: list[RetrievedPassage] = field(default_factory=list)
    seen: set[tuple[str | None, int | None, str]] = field(default_factory=set)

    def add(self, passage: RetrievedPassage) -> None:
        text = passage.text.strip()
        if not text:
            return
        key = (passage.citation, passage.paragraph, text[:160])
        if key in self.seen:
            return
        self.seen.add(key)
        self.passages.append(passage)

    def ranked(self, limit: int) -> tuple[RetrievedPassage, ...]:
        """Best-scoring first, then cap.

        The cap used to be applied to ARRIVAL order, and the orchestrator hands this
        function L1's results before L2's (``state.layers`` is populated L3, L1, L2).
        So L1's quote evidence -- a by-product of checking a quotation, not a retrieval
        result -- could displace the passages L2 actually ranked. An unscored passage
        sorts last rather than first, because "no score" is not a good score.
        """
        order = sorted(
            enumerate(self.passages),
            key=lambda item: (-(item[1].score if item[1].score is not None else -1.0), item[0]),
        )
        return tuple(passage for _, passage in order[:limit])


def passages_from_layer_results(results: Iterable[LayerResult]) -> tuple[RetrievedPassage, ...]:
    """Harvest the passages L2 actually retrieved, from its result.

    Two sources, in order of preference: an explicit ``detail["passages"]`` list, and
    the ``Evidence.best_match_text`` on its findings. The second is a fallback, not a
    design: a layer that reports what it matched is showing its working, which is the
    whole point of ``Evidence``.

    Collection order does not survive: the result is ranked by score before the cap,
    so which layer happened to be harvested first cannot decide what the judge reads.
    """
    from verifier.settings import settings

    harvest = _PassageHarvest()
    for result in results:
        for raw in result.detail.get("passages", ()) or ():
            if isinstance(raw, RetrievedPassage):
                harvest.add(raw)
            elif isinstance(raw, dict):
                text = raw.get("text") or raw.get("best_match_text") or ""
                harvest.add(
                    RetrievedPassage(
                        text=str(text),
                        citation=_opt_str(raw.get("citation")),
                        paragraph=_opt_int(raw.get("paragraph")),
                        paragraph_to=_opt_int(raw.get("paragraph_to")),
                        score=_opt_float(raw.get("score")),
                        source_url=_opt_str(raw.get("source_url")),
                    )
                )
            elif isinstance(raw, str):
                harvest.add(RetrievedPassage(text=raw))
        for finding in result.findings:
            if finding.evidence.best_match_text:
                harvest.add(
                    RetrievedPassage(
                        text=finding.evidence.best_match_text,
                        paragraph=finding.evidence.best_match_paragraph,
                        score=finding.evidence.score,
                        source_url=finding.evidence.source_url,
                    )
                )
    return harvest.ranked(settings.MAX_JUDGE_PASSAGES)


def _opt_str(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _render_uncited(propositions: tuple[str, ...]) -> str:
    """The assertions L1a could find no authority for, as a numbered list.

    L1a deliberately stops at "no citation is in scope for this sentence". Whether the
    authority cited elsewhere in the answer actually supports it is a reasoning
    question, and this is the layer permitted to answer one.
    """
    if not propositions:
        return ""
    return "\n".join(f"{index}. {text}" for index, text in enumerate(propositions, start=1))


def propositions_from_findings(findings: Sequence[Finding]) -> tuple[str, ...]:
    """Pull L1a's uncited assertions back out of the findings it produced.

    Reading them from the findings rather than threading the extraction result through
    the judge phase keeps ``run_judge_phase`` able to work from a restored ``RunState``
    alone -- the judge may run in a different process from the deterministic phase.
    """
    out: list[str] = []
    for finding in findings:
        if finding.code is FindingCode.PROPOSITION_UNCITED:
            text = finding.evidence.best_match_text
            if text:
                out.append(text)
        elif finding.code is FindingCode.OUTPUT_UNCITED:
            for item in finding.evidence.extra.get("propositions", []) or []:
                text = item.get("text") if isinstance(item, dict) else None
                if text:
                    out.append(str(text))
    return tuple(dict.fromkeys(out))
