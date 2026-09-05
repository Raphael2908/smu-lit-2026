"""Deterministic offline stand-ins for the summariser and the judge.

No network, no keys, no randomness. Two properties matter:

* **Deterministic.** The same input always produces the same output, so a threshold or
  a verdict never wobbles between test runs.
* **The judge is steerable.** ``MockJudge`` can pass, fail, or return each malformed
  shape the parse ladder is supposed to survive -- and it runs those shapes through the
  *real* ladder from ``openrouter_llm``, so the ladder is genuinely exercised offline
  rather than simulated.

``MockJudge.calls`` is what ``tests/pipeline/test_judge_cannot_launder.py`` asserts is
zero: the proof that a deterministic failure never reaches the model.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from verifier.contracts.documents import SourceDocument
from verifier.providers.anthropic_llm import sentence_split
from verifier.providers.base import (
    CitationCandidate,
    CitationExtraction,
    JudgeResult,
    JudgeRubric,
)
from verifier.providers.openrouter_llm import coerce_judge_result, unparseable_result

__all__ = ["MockCitationExtractor", "MockJudge", "MockSummariser"]


class MockSummariser:
    provider = "mock"

    def __init__(self, model: str | None = None) -> None:
        from verifier.settings import settings

        self.model = model or settings.SUMMARISER_MODEL
        self.calls = 0
        self.claim_calls = 0

    async def summarise_document(self, doc: SourceDocument) -> str:
        self.calls += 1
        name = doc.case_name or doc.neutral_citation or doc.source_url
        court = doc.court or "the court"
        # A digest keeps distinct documents distinguishable in the embedding cache
        # without ever depending on network content.
        digest = hashlib.sha256((doc.text or doc.source_url).encode("utf-8")).hexdigest()[:8]
        first = (doc.text or "").strip().split("\n", 1)[0][:200]
        return (
            f"{name} ({court}, {doc.year or 'n.d.'}) [mock:{digest}]. "
            f"A Singapore judgment. Opening text: {first}"
        ).strip()

    async def split_claims(self, text: str) -> list[str]:
        self.claim_calls += 1
        return sentence_split(text)


class MockCitationExtractor:
    """The regex extractor, wearing the extractor interface.

    This is NOT a stub returning canned strings, and it is not a production fallback.
    ``extraction.extract_citations`` is a real, heavily tested parser built against the
    SAL SLR Style Guide (docs/03-findings.md F13), so running it here means
    ``PROVIDER_MODE=mock`` still finds the citations in an answer with no key, no network
    and no tokens. That is what keeps the offline suite meaningful, ``make dev`` usable
    without secrets, and the demo a working product rather than an empty one.

    The same idea as ``MockSummariser.split_claims`` delegating to the real
    ``sentence_split``: mock the vendor, not the behaviour.

    ``citations`` overrides the regex entirely, for tests that need one specific
    candidate -- a fabricated citation, a foreign one, a string that is not in the
    output at all.
    """

    provider = "mock"

    def __init__(
        self,
        citations: list[CitationCandidate] | None = None,
        *,
        model: str | None = None,
        degraded: str | None = None,
    ) -> None:
        from verifier.settings import settings

        self.model = model or settings.EXTRACTOR_MODEL
        self.calls = 0
        self._citations = citations
        self._degraded = degraded

    async def extract_citations(self, ai_output: str) -> CitationExtraction:
        self.calls += 1
        if self._degraded is not None:
            return CitationExtraction(
                model=self.model, provider=self.provider, degraded=self._degraded
            )
        if self._citations is not None:
            found = tuple(self._citations)
        else:
            from verifier.extraction import extract_citations

            found = tuple(
                CitationCandidate(raw_text=c.raw_text, url=c.url)
                for c in extract_citations(ai_output)
            )
        return CitationExtraction(citations=found, model=self.model, provider=self.provider)


#: Malformed shapes the parse ladder must survive. Keyed by the rung expected to win.
_MALFORMED: dict[str, str] = {
    "fenced": (
        "Here is my assessment.\n\n"
        "```json\n"
        '{"passed": true, "rubric": {"factual_faithfulness": 4, "contextual_accuracy": 4,'
        ' "citation_integrity": 4, "responsiveness": 4}, "reasons": ["consistent with the'
        ' passages"]}\n'
        "```\n"
    ),
    "balanced": (
        "After reviewing the passages, my conclusion is: "
        '{"passed": false, "rubric": {"factual_faithfulness": 1, "contextual_accuracy": 3,'
        ' "citation_integrity": 3, "responsiveness": 4}, "reasons": ["the answer states a'
        ' proposition the cited paragraphs do not support"]} '
        "Let me know if you would like me to expand on any dimension."
    ),
    "garbage": (
        "I am unable to return structured output right now, but broadly the answer "
        "looks fine to me."
    ),
    "invalid_rubric": ('{"passed": true, "rubric": {"factual_faithfulness": 9}, "reasons": []}'),
}


class MockJudge:
    """A judge that never leaves the process.

    ``mode``:
      * ``pass``    -- a clean 4/4/4/4 verdict.
      * ``fail``    -- a rubric that trips JUDGE_FAILED_FAITHFULNESS.
      * ``fenced`` / ``balanced`` / ``garbage`` / ``invalid_rubric`` -- raw text driven
        through the real parse ladder, so the ladder itself is under test.

    ``repair_to`` makes the single repair retry observable: the first call returns the
    malformed body, the second returns that shape instead.
    """

    provider = "mock"

    def __init__(
        self,
        *,
        mode: str = "pass",
        model: str = "mock-judge",
        rubric: JudgeRubric | None = None,
        reasons: list[str] | None = None,
        passed: bool | None = None,
        repair_to: str | None = None,
        raw_response: str | None = None,
        latency_ms: int = 1,
    ) -> None:
        self.model = model
        self.mode = mode
        self.calls = 0
        self.last_system_prompt: str | None = None
        self.last_payload: dict[str, Any] | None = None
        self.systems: list[str] = []
        self._rubric = rubric
        self._reasons = reasons
        self._passed = passed
        self._repair_to = repair_to
        self._raw_response = raw_response
        self._latency_ms = latency_ms

    async def judge(self, *, system_prompt: str, payload: dict) -> JudgeResult:
        self.calls += 1
        self.last_system_prompt = system_prompt
        self.last_payload = dict(payload)
        self.systems.append(system_prompt)
        # A tiny yield keeps the mock honest about being a coroutine boundary.
        started = time.perf_counter()

        mode = self.mode
        if self._repair_to and self.calls > 1:
            mode = self._repair_to

        if self._raw_response is not None:
            return self._from_raw(self._raw_response, started)
        if mode in _MALFORMED:
            return self._from_raw(_MALFORMED[mode], started)
        if mode == "fail":
            rubric = self._rubric or JudgeRubric(
                factual_faithfulness=1,
                contextual_accuracy=3,
                citation_integrity=3,
                responsiveness=4,
            )
            passed = self._passed if self._passed is not None else False
            reasons = self._reasons or [
                "The answer states the test more broadly than the cited paragraphs hold."
            ]
        else:
            rubric = self._rubric or JudgeRubric(
                factual_faithfulness=4,
                contextual_accuracy=4,
                citation_integrity=4,
                responsiveness=4,
            )
            passed = self._passed if self._passed is not None else True
            reasons = self._reasons or ["Every proposition is supported by the passages."]

        return JudgeResult(
            passed=passed,
            rubric=rubric,
            reasons=list(reasons),
            raw_response="",
            parse_path="strict",
            latency_ms=self._latency_ms or int((time.perf_counter() - started) * 1000),
            model=self.model,
            provider=self.provider,
        )

    def _from_raw(self, raw: str, started: float) -> JudgeResult:
        elapsed = int((time.perf_counter() - started) * 1000)
        result, _ = coerce_judge_result(
            raw, model=self.model, provider=self.provider, latency_ms=elapsed
        )
        if result is not None:
            return result
        return unparseable_result(
            raw, model=self.model, provider=self.provider, latency_ms=elapsed, retries=1
        )
