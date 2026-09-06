"""L0 -- preprocessing. The one gate that runs on model output.

L0 reads the answer once with Claude Haiku, lists every citation it offers, and asks the
question that precedes every other one: **does this answer cite anything at all?** An
output can be entirely free of fabricated citations by citing nothing whatsoever, and
every later check only ever examines authority the answer actually offered.

WHY THIS IS NOT L1's PROBLEM ANY MORE.

This check used to be sub-check 1a of Layer 1, and that was the one thing wrong with an
otherwise honest layer. L1 is badged deterministic -- a closed index and three tier
lists, no model, same answer twice -- and 1a counted what an LLM returned. One
non-deterministic sub-check is enough to defeat the badge for the whole layer, and the
badge is the argument: a verifier whose citation check is itself a language model has
only moved the question. Moving citedness to L0 costs nothing (it needed no fetch, no
resolution and no list lookup -- only the extraction it now sits next to) and leaves L1
able to make its claim truthfully.

So the model call and the verdict it supports are now in the same place, named, ahead of
everything, and reported on its own row. Where a model reads the answer is a fact about
this system a reader is entitled to see rather than discover.

L0 CANNOT BE SKIPPED AND ITS FAILURE ENDS THE RUN. L1-L3 all consume its output; there
is nothing for them to check if it did not produce one. That is why this is a gate
rather than a fifth score: it has no number, and it stops the pipeline instead.

TWO FAILURES, DELIBERATELY DIFFERENT.

``OUTPUT_UNCITED`` says the answer asserts law and offers no authority. It is reached by
counting, not judging.

``PREPROCESSING_FAILED`` says we could not read the answer -- the extractor timed out,
had no key, or returned something unparseable. Both stop the run; they must never share
a code, because one is a statement about the answer and the other is a statement about
our afternoon. See ``docs/03-findings.md`` F12 and ``todo.md`` bug 5 for the standing
cost of failing on the second.
"""

from __future__ import annotations

from verifier.contracts.citations import ExtractedProposition
from verifier.contracts.enums import (
    AuthorityKind,
    FindingCode,
    Layer,
    LayerStatus,
    PropositionKind,
    Severity,
)
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.layers.base import BaseLayer, status_from_findings
from verifier.settings import Settings, get_settings

__all__ = ["PreprocessingLayer"]

#: The lead-in for a per-assertion warning, by why the sentence needed authority.
_PROPOSITION_MESSAGE: dict[PropositionKind, str] = {
    PropositionKind.HOLDING: "This states what a court decided",
    PropositionKind.LEGAL_TEST: "This states what the law requires",
    PropositionKind.ESTABLISHED: "This appeals to settled law",
    PropositionKind.STATUTE: "This states a statutory rule",
}


class PreprocessingLayer(BaseLayer):
    """L0. Reads ``data.extraction`` and nothing else.

    Pure with respect to ``LayerInput``: the extraction itself happens upstream (the
    orchestrator owns the model call so it can run the claim split alongside it), and
    this layer only reaches a verdict over the result. That split is what makes every
    case below testable by constructing an ``ExtractionResult`` and nothing else.
    """

    layer = Layer.L0_PREPROCESSING

    async def _run(self, data: LayerInput) -> LayerResult:
        settings = get_settings()
        extraction = data.extraction
        counts: dict[str, int] = {
            "propositions": len(extraction.propositions),
            "propositions_cited": 0,
            "propositions_uncited": 0,
            "authorities": extraction.authority_count,
        }

        if extraction.extractor_degraded:
            # Checked BEFORE the propositions, because a degraded extraction has no
            # propositions to check -- falling through would report the run as clean.
            findings: tuple[Finding, ...] = (
                self._preprocessing_failed(data, extraction.extractor_degraded),
            )
            ran = True
        else:
            # "Nothing was asserted" and "the gate is switched off" are both reasons the
            # gate did not RUN, and neither is a reason to say it passed.
            ran = settings.L0_CITEDNESS_ENABLED and bool(extraction.propositions)
            findings = tuple(self._check_propositions(data, counts, settings))

        if not ran:
            # A procedural answer, a clarifying question, a refusal -- or the gate turned
            # off deliberately. NOT_APPLICABLE, not PASS: a check that never ran must
            # never read as clearance, which is the governing rule of this whole stack.
            return LayerResult(
                layer=self.layer,
                status=LayerStatus.NOT_APPLICABLE,
                detail=self._detail(data, counts),
            )

        return LayerResult(
            layer=self.layer,
            status=status_from_findings(findings),
            findings=findings,
            # No score. L0 asks a yes/no question over a count; putting an unmeasured
            # 0-1 here would sit in the same panel slot as L2's calibrated cosine.
            detail=self._detail(data, counts),
        )

    # -- detail ------------------------------------------------------------------------

    def _detail(self, data: LayerInput, counts: dict[str, int]) -> dict[str, object]:
        """What L0 found, in full.

        The citation LIST, not just a count: the whole point of putting a model here is
        that a reader can see what it decided the answer cited, and the unchecked
        leftovers beside it.
        """
        extraction = data.extraction
        detail: dict[str, object] = {
            **counts,
            "clusters": len(extraction.clusters),
            "quotes": len(extraction.quotes),
            "statutes": len(extraction.statutes),
            "explicit_domains": list(extraction.explicit_domains),
            "citations": [
                {
                    "ordinal": cluster.ordinal,
                    "text": cluster.preferred.raw_text,
                    "type": cluster.preferred.citation_type.value,
                    "url": cluster.preferred.url,
                }
                for cluster in extraction.clusters
            ],
            "untyped": list(extraction.untyped),
        }
        if extraction.extractor_degraded:
            detail["extractor_degraded"] = extraction.extractor_degraded
        return detail

    # -- the gate ----------------------------------------------------------------------

    def _check_propositions(
        self,
        data: LayerInput,
        counts: dict[str, int],
        settings: Settings,
    ) -> list[Finding]:
        """Whether the output's legal assertions rest on any authority.

        Two findings, doing deliberately different jobs.

        ``OUTPUT_UNCITED`` is the FAIL, and it is reached by counting, not judging: the
        output asserts law and contains no citation and no specific statutory reference
        anywhere. No attribution question arises, so none of the "which citation covers
        which sentence" uncertainty that makes the other finding a WARN applies here.

        ``PROPOSITION_UNCITED`` is per-assertion and WARN by default. Coverage is
        generous by construction, so a proposition reported here had no authority
        anywhere in its scope -- but "scope" is still a heuristic over prose that has no
        fixed citation structure, and a heuristic must not be able to fail a run.
        """
        if not settings.L0_CITEDNESS_ENABLED:
            return []
        propositions = data.extraction.propositions
        if not propositions:
            return []

        uncited = [p for p in propositions if not p.is_cited]
        counts["propositions_cited"] = len(propositions) - len(uncited)
        counts["propositions_uncited"] = len(uncited)
        if not uncited:
            return []

        authorities = data.extraction.authority_count
        if authorities == 0 and len(uncited) >= settings.L0_MIN_ASSERTIONS_FOR_FAIL:
            return [self._output_uncited(data, uncited, propositions)]

        severity = Severity.WARN if settings.L0_UNCITED_SEVERITY == "warn" else Severity.INFO
        return [self._proposition_uncited(data, p, severity, authorities) for p in uncited]

    def _preprocessing_failed(self, data: LayerInput, reason: str) -> Finding:
        """The extractor did not run, so nothing downstream has anything to check.

        NOT downgraded on a follow-up turn. The follow-up rule exists because authority
        may legitimately sit in an earlier turn of the conversation; an outage is an
        outage in any turn, and there is no earlier turn that supplies the missing read.
        """
        return Finding(
            id=f"{data.run_id}:L0:extract:{FindingCode.PREPROCESSING_FAILED.value}",
            layer=self.layer,
            code=FindingCode.PREPROCESSING_FAILED,
            severity=Severity.FAIL,
            message=(
                "This answer could not be read for citations, so nothing in it was "
                f"verified: {reason}. This is a failure of the checker, not a finding "
                "about the answer -- re-run once the extractor is available."
            ),
            evidence=Evidence(extra={"reason": reason}),
        )

    def _output_uncited(
        self,
        data: LayerInput,
        uncited: list[ExtractedProposition],
        propositions: tuple[ExtractedProposition, ...],
    ) -> Finding:
        """The whole-output finding: law asserted, nothing cited, anywhere.

        On a FOLLOW-UP turn this is a WARN instead, and a WARN does not stop the run.
        "What about the second limb?" answers a question whose authority was established
        in the previous turn, and demanding that the answer re-cite it would make the
        single most common shape of real conversation fail. This is the same reasoning
        that makes L3 downgrade a follow-up, and for the same reason: under fail-fast a
        false red is unrecoverable.
        """
        first = uncited[0]
        severity = Severity.WARN if data.is_followup else Severity.FAIL
        followup_note = (
            " This is a follow-up turn, so the authority may have been given earlier in "
            "the conversation; it is reported rather than failed."
            if data.is_followup
            else ""
        )
        return Finding(
            id=f"{data.run_id}:L0:output:{FindingCode.OUTPUT_UNCITED.value}",
            layer=self.layer,
            code=FindingCode.OUTPUT_UNCITED,
            severity=severity,
            message=(
                f"This answer states the law but cites no authority at all: "
                f"{len(uncited)} assertion{'s' if len(uncited) != 1 else ''} with no case, "
                f"statute or source behind {'them' if len(uncited) != 1 else 'it'}." + followup_note
            ),
            output_span=first.span,
            evidence=Evidence(
                best_match_text=first.text,
                extra={
                    "assertions": len(propositions),
                    "uncited": len(uncited),
                    "authorities": 0,
                    "is_followup": data.is_followup,
                    "kinds": sorted({p.kind.value for p in uncited}),
                    # Every uncited assertion, so the panel can highlight all of them
                    # under one finding rather than repeating the same message N times.
                    "propositions": [
                        {
                            "ordinal": p.ordinal,
                            "kind": p.kind.value,
                            "cue": p.cue,
                            "text": p.text,
                            "span": {"start": p.span.start, "end": p.span.end},
                        }
                        for p in uncited
                    ],
                },
            ),
        )

    def _proposition_uncited(
        self,
        data: LayerInput,
        proposition: ExtractedProposition,
        severity: Severity,
        authorities: int,
    ) -> Finding:
        return Finding(
            id=f"{data.run_id}:L0:prop:{proposition.ordinal}:"
            f"{FindingCode.PROPOSITION_UNCITED.value}",
            layer=self.layer,
            code=FindingCode.PROPOSITION_UNCITED,
            severity=severity,
            message=(
                f"{_PROPOSITION_MESSAGE[proposition.kind]} but no authority is cited for it. "
                "The output cites elsewhere, so this may be an omission rather than an "
                "invention."
            ),
            output_span=proposition.span,
            evidence=Evidence(
                best_match_text=proposition.text,
                extra={
                    "kind": proposition.kind.value,
                    "cue": proposition.cue,
                    "authority": AuthorityKind.NONE.value,
                    "authorities_in_output": authorities,
                },
            ),
        )
