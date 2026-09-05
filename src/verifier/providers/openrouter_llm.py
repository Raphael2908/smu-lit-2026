"""OpenRouter judge provider, plus the JSON parse ladder every judge provider shares.

Raw ``httpx`` against one endpoint. Pulling the OpenAI SDK in to POST to a single URL
would add a dependency, a client abstraction and a set of retry semantics we would then
have to reason about, in exchange for nothing.

**Never assume schema enforcement.** ``response_format: json_schema`` with
``strict: true`` is requested, but OpenRouter forwards it to whichever provider serves
the model and support varies -- some enforce it, some treat it as a hint, some ignore
it. A judge that raises on a fenced code block is a judge that fails open on a bad day,
so the parse ladder below is the real contract and the schema is an optimisation:

    strict JSON -> fenced ```json block -> first balanced {...} -> ONE repair retry
    -> give up (JUDGE_UNPARSEABLE)

Which rung succeeded is recorded in ``JudgeResult.parse_path``, so a drift in provider
behaviour shows up as a metric rather than as a mystery.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from verifier.errors import FatalError, ProviderKeyMissing, RetryableError
from verifier.providers.base import JudgeResult, JudgeRubric

__all__ = [
    "PARSE_PATH_UNPARSEABLE",
    "OPENROUTER_URL",
    "OpenRouterJudge",
    "JudgeValidationError",
    "coerce_judge_result",
    "judge_json_schema",
    "parse_judge_payload",
    "validate_judge_object",
]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

#: The sentinel ``JudgeResult.parse_path`` carries when every rung of the ladder failed.
#: L5 maps this to JUDGE_UNPARSEABLE / Severity.WARN.
PARSE_PATH_UNPARSEABLE = "unparseable"

RUBRIC_DIMENSIONS = (
    "factual_faithfulness",
    "contextual_accuracy",
    "citation_integrity",
    "responsiveness",
)

_USER_TURN = (
    "Assess the answer against the passages above and return ONLY the JSON object "
    "described. No preamble, no code fence, no commentary."
)


class JudgeValidationError(ValueError):
    """The response parsed as JSON but is not a judge verdict."""


def judge_json_schema() -> dict[str, Any]:
    """The response schema. ``additionalProperties: false`` + full ``required`` is what
    strict-mode providers need; lenient ones ignore all of it."""
    return {
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "rubric": {
                "type": "object",
                "properties": {
                    dim: {"type": "integer", "minimum": 0, "maximum": 4}
                    for dim in RUBRIC_DIMENSIONS
                },
                "required": list(RUBRIC_DIMENSIONS),
                "additionalProperties": False,
            },
            "reasons": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["passed", "rubric", "reasons"],
        "additionalProperties": False,
    }


# --- the parse ladder -------------------------------------------------------------


def _try_strict(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _try_fenced(text: str) -> dict[str, Any] | None:
    """Recover ```json ... ``` (and a bare ``` fence). Models add fences even when told
    not to; that is a formatting quirk, not a failed judgement."""
    fence = "```"
    start = text.find(fence)
    while start != -1:
        after = text.find("\n", start)
        if after == -1:
            return None
        end = text.find(fence, after)
        if end == -1:
            return None
        candidate = text[after + 1 : end].strip()
        obj = _try_strict(candidate)
        if obj is not None:
            return obj
        start = text.find(fence, end + len(fence))
    return None


def _try_balanced(text: str) -> dict[str, Any] | None:
    """Scan for the first balanced ``{...}``, respecting strings and escapes.

    This is the rung that survives trailing prose ("Here is my assessment: {...}
    Let me know if you'd like more detail.") -- the single most common shape when a
    provider silently drops the response_format.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start != -1:
                    obj = _try_strict(text[start : index + 1])
                    if obj is not None:
                        return obj
                    start = -1
    return None


def parse_judge_payload(text: str) -> tuple[dict[str, Any] | None, str]:
    """Run the ladder. Returns ``(object_or_none, parse_path)``."""
    if not text or not text.strip():
        return None, PARSE_PATH_UNPARSEABLE
    for path, parser in (
        ("strict", _try_strict),
        ("fenced", _try_fenced),
        ("balanced", _try_balanced),
    ):
        obj = parser(text)
        if obj is not None:
            return obj, path
    return None, PARSE_PATH_UNPARSEABLE


def validate_judge_object(obj: dict[str, Any]) -> tuple[bool, JudgeRubric, list[str]]:
    """Validate a parsed object into ``(passed, rubric, reasons)``.

    Tolerant where tolerance is safe (a string "3", a missing ``reasons``) and strict
    where it is not (a missing or out-of-range rubric dimension). A rubric we cannot
    trust must become JUDGE_UNPARSEABLE, not a fabricated score.
    """
    rubric_raw = obj.get("rubric")
    if not isinstance(rubric_raw, dict):
        raise JudgeValidationError("missing object field 'rubric'")

    scores: dict[str, int] = {}
    for dim in RUBRIC_DIMENSIONS:
        if dim not in rubric_raw:
            raise JudgeValidationError(f"rubric is missing '{dim}'")
        try:
            value = int(rubric_raw[dim])
        except (TypeError, ValueError) as exc:
            raise JudgeValidationError(f"rubric.{dim} is not an integer") from exc
        if not 0 <= value <= 4:
            raise JudgeValidationError(f"rubric.{dim} must be 0-4, got {value}")
        scores[dim] = value

    passed_raw = obj.get("passed")
    if isinstance(passed_raw, bool):
        passed = passed_raw
    elif isinstance(passed_raw, str) and passed_raw.strip().lower() in {"true", "false"}:
        passed = passed_raw.strip().lower() == "true"
    else:
        raise JudgeValidationError("missing boolean field 'passed'")

    reasons_raw = obj.get("reasons") or []
    if isinstance(reasons_raw, str):
        reasons = [reasons_raw]
    elif isinstance(reasons_raw, list):
        reasons = [str(r) for r in reasons_raw if str(r).strip()]
    else:
        reasons = []

    return passed, JudgeRubric(**scores), reasons


def coerce_judge_result(
    text: str,
    *,
    model: str,
    provider: str,
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    retries: int = 0,
) -> tuple[JudgeResult | None, str | None]:
    """Ladder + validation in one step.

    Returns ``(result, error)``. A non-None ``error`` is the message to append to a
    single repair retry; if the caller has already retried it should build the
    unparseable result instead.
    """
    obj, path = parse_judge_payload(text)
    if obj is None:
        return None, "the response contained no JSON object"
    try:
        passed, rubric, reasons = validate_judge_object(obj)
    except JudgeValidationError as exc:
        return None, str(exc)
    return (
        JudgeResult(
            passed=passed,
            rubric=rubric,
            reasons=reasons,
            raw_response=text,
            parse_path=path,
            retries=retries,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            model=model,
            provider=provider,
        ),
        None,
    )


def unparseable_result(
    text: str,
    *,
    model: str,
    provider: str,
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    retries: int = 0,
) -> JudgeResult:
    """Fail OPEN: ``passed=True`` with no rubric.

    A judge we could not read has not convicted anyone. L5 turns the missing rubric
    into JUDGE_UNPARSEABLE at Severity.WARN, which annotates without failing.
    """
    return JudgeResult(
        passed=True,
        rubric=None,
        reasons=[],
        raw_response=text,
        parse_path=PARSE_PATH_UNPARSEABLE,
        retries=retries,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        model=model,
        provider=provider,
    )


# --- the provider -----------------------------------------------------------------


class OpenRouterJudge:
    """``Judge`` implementation over OpenRouter's OpenAI-compatible endpoint."""

    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = OPENROUTER_URL,
        timeout: float = 90.0,
        max_tokens: int = 4096,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        from verifier.settings import settings

        key = api_key if api_key is not None else settings.OPENROUTER_API_KEY
        if not key or not key.strip():
            # Never fall back to a mock silently: a verifier that quietly stops
            # verifying is worse than one that stops.
            raise ProviderKeyMissing("OpenRouter", "OPENROUTER_API_KEY")
        self._api_key = key
        self.model = model or settings.JUDGE_MODEL
        self._base_url = base_url
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers; harmless elsewhere.
            "X-Title": "sal-verifier",
        }

    def _body(self, system_prompt: str, user_turn: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_turn},
            ],
            "max_tokens": self._max_tokens,
            # Requested, never relied upon -- see the module docstring.
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "faithfulness_verdict",
                    "strict": True,
                    "schema": judge_json_schema(),
                },
            },
            # Ask OpenRouter to report what the call cost, so the run can price itself.
            "usage": {"include": True},
        }

    async def judge(self, *, system_prompt: str, payload: dict) -> JudgeResult:
        started = time.perf_counter()
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            text, cost = await self._post(client, self._body(system_prompt, _USER_TURN))
            elapsed = int((time.perf_counter() - started) * 1000)
            result, error = coerce_judge_result(
                text,
                model=self.model,
                provider=self.provider,
                latency_ms=elapsed,
                cost_usd=cost,
            )
            if result is not None:
                return result

            # ONE repair retry, with the validation error appended so the model is told
            # exactly what was wrong. More than one turns a bad response into a
            # multi-minute, multi-dollar loop for no measured benefit.
            repair_turn = (
                f"{_USER_TURN}\n\nYour previous response could not be used: {error}. "
                "Return the JSON object only."
            )
            retry_text, retry_cost = await self._post(
                client, self._body(system_prompt, repair_turn)
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            retried, _ = coerce_judge_result(
                retry_text,
                model=self.model,
                provider=self.provider,
                latency_ms=elapsed,
                cost_usd=cost + retry_cost,
                retries=1,
            )
            if retried is not None:
                return retried
            return unparseable_result(
                retry_text or text,
                model=self.model,
                provider=self.provider,
                latency_ms=elapsed,
                cost_usd=cost + retry_cost,
                retries=1,
            )
        finally:
            if owns_client:
                await client.aclose()

    async def _post(self, client: httpx.AsyncClient, body: dict[str, Any]) -> tuple[str, float]:
        try:
            response = await client.post(self._base_url, headers=self._headers(), json=body)
        except httpx.TimeoutException as exc:
            raise RetryableError(f"OpenRouter timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RetryableError(f"OpenRouter transport error: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableError(f"OpenRouter returned {response.status_code}: {response.text}")
        if response.status_code >= 400:
            raise FatalError(f"OpenRouter returned {response.status_code}: {response.text}")

        data = response.json()
        return _first_message_text(data), _cost_of(data)


def _first_message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    # Some providers return the OpenAI "content parts" shape.
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _cost_of(data: dict[str, Any]) -> float:
    usage = data.get("usage") or {}
    try:
        return float(usage.get("cost", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
