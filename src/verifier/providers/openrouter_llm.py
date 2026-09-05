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
import re
import time
from typing import Any

import httpx

from verifier.contracts.documents import SourceDocument
from verifier.errors import FatalError, ProviderKeyMissing, RetryableError
from verifier.providers.base import CitationExtraction, JudgeResult, JudgeRubric

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


def parse_json_payload(text: str) -> tuple[dict[str, Any] | None, str]:
    """Run the ladder over any JSON response. Returns ``(object_or_none, parse_path)``.

    Judge-independent, so the citation extractor gets the same three rungs rather than a
    second, subtly different parser. Structured output is an optimisation; the ladder is
    the contract, and there should be exactly one of it.
    """
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


def parse_judge_payload(text: str) -> tuple[dict[str, Any] | None, str]:
    """Run the ladder. Returns ``(object_or_none, parse_path)``."""
    return parse_json_payload(text)


#: The dimensions the ACTIVE prompt scores. Binary, unlike the legacy 0-4 set.
BINARY_DIMENSIONS = ("correctness", "material_completeness")


def _binary_verdict(obj: dict[str, Any]) -> tuple[bool, JudgeRubric, list[str]] | None:
    """Read a binary verdict, from the top level or from inside ``rubric``.

    WHY THIS RUNG EXISTS. The active prompt asks for prose verdict lines, so
    ``parse_native_verdict`` normally wins and this is never reached. But a model that
    answers in JSON instead -- which several do, unprompted -- produced
    ``{"correctness": 0, "material_completeness": 0, "defects": [...]}``, and the legacy
    validator below rejected it for lacking a ``rubric`` object of four 0-4 scores that
    the current prompt never asks for. The verdict was discarded, L5 fell open, and a
    conviction was recorded as a pass. Observed live on claude-sonnet-5.

    A dimension is only read when present; scoring nothing is not scoring zero.
    """
    for source in (obj, obj.get("rubric") if isinstance(obj.get("rubric"), dict) else None):
        if not isinstance(source, dict):
            continue
        if not any(dim in source for dim in BINARY_DIMENSIONS):
            continue
        scores: dict[str, int] = {}
        for dim in BINARY_DIMENSIONS:
            if dim not in source:
                continue
            try:
                value = int(source[dim])
            except (TypeError, ValueError) as exc:
                raise JudgeValidationError(f"{dim} is not an integer") from exc
            if value not in (0, 1):
                raise JudgeValidationError(f"{dim} must be 0 or 1, got {value}")
            scores[dim] = value
        if len(scores) != len(BINARY_DIMENSIONS):
            missing = [d for d in BINARY_DIMENSIONS if d not in scores]
            raise JudgeValidationError(f"binary verdict is missing {missing[0]!r}")
        passed_raw = obj.get("passed")
        if isinstance(passed_raw, bool):
            passed = passed_raw
        else:
            # Both dimensions must hold, matching coerce_judge_result's native path.
            passed = all(v == 1 for v in scores.values())
        return passed, JudgeRubric(**scores), _reasons_from(obj)
    return None


def _reasons_from(obj: dict[str, Any]) -> list[str]:
    """Pull the stated defects out, whatever the model chose to call the field."""
    for key in ("reasons", "defects", "errors", "incorrect_propositions"):
        raw = obj.get(key)
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        if isinstance(raw, list):
            out: list[str] = []
            for item in raw:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif isinstance(item, dict):
                    parts = [
                        str(v).strip()
                        for k, v in item.items()
                        if k in ("type", "defect", "description", "explanation", "detail")
                        and str(v).strip()
                    ]
                    if parts:
                        out.append(" - ".join(parts))
            if out:
                return out
    return []


def validate_judge_object(obj: dict[str, Any]) -> tuple[bool, JudgeRubric, list[str]]:
    """Validate a parsed object into ``(passed, rubric, reasons)``.

    Tolerant where tolerance is safe (a string "3", a missing ``reasons``) and strict
    where it is not (a missing or out-of-range rubric dimension). A rubric we cannot
    trust must become JUDGE_UNPARSEABLE, not a fabricated score.

    The BINARY shape is tried first because it is what the active prompt asks for; the
    0-4 block below serves a differently prompted judge.
    """
    binary = _binary_verdict(obj)
    if binary is not None:
        return binary

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
    # The active prompt returns prose with explicit verdict markers, so try that
    # first. Only if it is absent do we fall through to the JSON ladder, which serves
    # a differently prompted judge.
    native = parse_native_verdict(text)
    if native is not None:
        correctness, completeness, defects = native
        return (
            JudgeResult(
                # Both dimensions must hold. They are assessed independently because
                # an answer can be entirely true and still materially misleading by
                # omission.
                passed=bool(correctness) and bool(completeness),
                rubric=JudgeRubric(correctness=correctness, material_completeness=completeness),
                reasons=defects,
                raw_response=text,
                parse_path="native",
                retries=retries,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                model=model,
                provider=provider,
            ),
            None,
        )

    obj, path = parse_judge_payload(text)
    if obj is None:
        return None, (
            "the response contained neither the required 'Correctness = 0/1' and "
            "'Material completeness = 0/1' lines nor a JSON object"
        )
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


def _prompt_defines_its_own_format(system_prompt: str) -> bool:
    """Whether the prompt already specifies its output contract.

    Detected rather than configured: a setting for this is one more thing to forget,
    and forgetting it silently produces a judge whose carefully specified verdict
    format is overridden by ours.
    """
    lowered = system_prompt.lower()
    return "material completeness" in lowered and "correctness" in lowered


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
        max_tokens: int | None = None,
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
        # Read from settings, not a literal default. A judge cut off mid-verdict is
        # reported as unparseable and L5 fails open, so an output budget that is too
        # small does not look like a truncation -- it looks like an acquittal.
        self._max_tokens = settings.JUDGE_MAX_TOKENS if max_tokens is None else max_tokens
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers; harmless elsewhere.
            "X-Title": "sal-verifier",
        }

    def _body(self, system_prompt: str, user_turn: str) -> dict[str, Any]:
        """Build the request.

        A JSON schema is requested ONLY when the system prompt does not define its own
        output contract. The active prompt ends by specifying exactly what to emit
        ("**Correctness = 0 or 1**"), and sending a competing `response_format`
        overrides it -- observed live: the model returned the schema's fields and
        ignored the prompt's, so the judge's own rubric never arrived. Two output
        contracts in one request means the prompt loses.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_turn},
            ],
            "max_tokens": self._max_tokens,
            # Ask OpenRouter to report what the call cost, so the run can price itself.
            "usage": {"include": True},
        }
        if not _prompt_defines_its_own_format(system_prompt):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "faithfulness_verdict",
                    "strict": True,
                    "schema": judge_json_schema(),
                },
            }
        return body

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
                "End your response with the two required lines exactly: "
                "'**Correctness = 0 or 1**' and '**Material completeness = 0 or 1**'."
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


# --- Summariser -------------------------------------------------------------------

#: Kept short on purpose. This summary is prefixed onto EVERY chunk of the judgment
#: before embedding, so each token here is paid once per chunk -- and voyage-law-2's
#: 16K context is already tight against a ~21K-token judgment.
_SUMMARY_SYSTEM = """You summarise Singapore judgments for a retrieval index.

Return at most 220 words of plain prose covering: the court and year, the parties, the
issue, the holding, and the ratio. No preamble, no markdown, no bullet points.

State only what the judgment says. If something is not in the text, leave it out --
this summary is attached to every chunk of the document and an invention here
propagates into every retrieval decision made about it."""

_CLAIM_SYSTEM = """You split legal writing into atomic factual claims.

Return a JSON array of strings and nothing else. Each element is one assertion, quoted
verbatim from the input where possible. Do not merge two assertions, do not invent any,
and do not add commentary.

Every claim must STAND ALONE. A reader who sees only that one string, with no access to
the rest of the answer, must be able to tell what is being asserted about what. In
particular:

- Never cut a qualifying clause away from the proposition it qualifies. Splitting
  "Policy considerations are applied only at the second stage, once a prima facie duty
  of care has been established" into "Policy considerations are applied only at the
  second stage" produces a string that does not say the second stage OF WHAT. Keep it
  as one claim.
- Resolve pronouns and bare demonstratives against the sentence they came from: "it",
  "this test", "the court" become the thing they refer to.
- Prefer one longer self-contained claim over two fragments. Splitting is only useful
  where each half is separately checkable against a source.

A fragment that cannot be checked on its own is worse than no split at all."""


class OpenRouterSummariser:
    """``Summariser`` over OpenRouter. Defaults to Haiku: this runs once per document
    and once per output, so it is on the latency path but not the accuracy path."""

    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = OPENROUTER_URL,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        from verifier.settings import settings

        key = api_key if api_key is not None else settings.OPENROUTER_API_KEY
        if not key or not key.strip():
            raise ProviderKeyMissing("OpenRouter", "OPENROUTER_API_KEY")
        self._api_key = key
        self.model = model or settings.SUMMARISER_MODEL
        self._base_url = base_url
        self._timeout = timeout
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": "sal-verifier",
        }

    async def _complete(self, system: str, user: str, max_tokens: int) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(self._base_url, headers=self._headers(), json=body)
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableError(f"OpenRouter {response.status_code}: {response.text[:200]}")
            if response.status_code >= 400:
                raise FatalError(f"OpenRouter {response.status_code}: {response.text[:200]}")
            return _first_message_text(response.json())
        finally:
            if owns_client:
                await client.aclose()

    async def summarise_document(self, doc: SourceDocument) -> str:
        from verifier.settings import settings

        # Send the head of the judgment: the court, parties, issue and holding are
        # established early, and sending 110k characters to summarise would cost more
        # than the rest of the run put together.
        head = doc.text[:24_000]
        label = doc.neutral_citation or doc.case_name or doc.source_url
        return (
            await self._complete(
                _SUMMARY_SYSTEM,
                f"Judgment: {label}\n\n{head}",
                max_tokens=max(256, settings.SUMMARY_MAX_TOKENS + 64),
            )
        ).strip()

    async def split_claims(self, text: str) -> list[str]:
        """Atomic claims, with a caller-side fallback if the model does not comply.

        Returning [] rather than raising is deliberate: ``semantic/chunking.py`` falls
        back to deterministic sentence windows, and a summariser hiccup must not fail a
        verification.
        """
        raw = await self._complete(_CLAIM_SYSTEM, text, max_tokens=2048)
        parsed, _path = parse_judge_payload(raw)
        if isinstance(parsed, list):
            return [str(c).strip() for c in parsed if str(c).strip()]
        try:
            candidate = json.loads(raw[raw.index("[") : raw.rindex("]") + 1])
            if isinstance(candidate, list):
                return [str(c).strip() for c in candidate if str(c).strip()]
        except Exception:  # noqa: BLE001 - the deterministic fallback covers this
            pass
        return []


# --- Native verdict format --------------------------------------------------------
#
# The active judge prompt returns prose, not JSON:
#
#     **Correctness = 0**
#     **Material completeness = 1**
#     1. **Incorrect — Overgeneralised:** ...
#
# We parse that rather than forcing a JSON schema onto it. Constraining a long
# reasoning prompt to emit structured output measurably degrades the reasoning, and
# this format costs one regex to read. The JSON ladder stays as the fallback so a
# differently prompted judge still works.

_DIMENSION_RE = re.compile(
    r"\*{0,2}\s*(?P<name>correctness|material\s+completeness)\s*\*{0,2}\s*[=:]\s*\*{0,2}\s*(?P<value>[01])",
    re.IGNORECASE,
)
#: Numbered or bulleted defect entries, e.g. "1. **Incorrect — Overgeneralised:** ..."
_DEFECT_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(?P<body>\*\*.+)$", re.MULTILINE)


def parse_native_verdict(text: str) -> tuple[int, int, list[str]] | None:
    """Read ``Correctness`` / ``Material completeness`` and the listed defects.

    Returns ``None`` when the markers are absent, so the caller can fall through to
    the JSON ladder. Both dimensions must be present: a response carrying only one is
    truncated or off-format, and guessing the other would invent a verdict.
    """
    found: dict[str, int] = {}
    for match in _DIMENSION_RE.finditer(text):
        key = (
            "material_completeness"
            if "completeness" in match.group("name").lower()
            else "correctness"
        )
        # First occurrence wins: the prompt states the verdict up front, and the
        # instructions themselves may be echoed back below it.
        found.setdefault(key, int(match.group("value")))
    if "correctness" not in found or "material_completeness" not in found:
        return None

    defects = [
        " ".join(m.group("body").replace("**", "").split())[:600] for m in _DEFECT_RE.finditer(text)
    ]
    return found["correctness"], found["material_completeness"], defects


class OpenRouterCitationExtractor:
    """``CitationExtractor`` over OpenRouter.

    Exists because the rest of this stack runs on an OpenRouter key, and an extractor
    that only speaks to Anthropic would leave a working real-mode deployment with no
    citations at all -- L0 permanently degraded, every run reporting that nothing could
    be checked. Same model, same prompt, different door.

    ``temperature=0`` for the same reason the Anthropic path pins it: the same answer
    must extract the same citations twice (docs/03-findings.md F17).
    """

    provider = "openrouter"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = OPENROUTER_URL,
        timeout: float | None = None,
        max_tokens: int = 4096,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        from verifier.settings import settings

        key = api_key if api_key is not None else settings.OPENROUTER_API_KEY
        if not key or not key.strip():
            raise ProviderKeyMissing("OpenRouter", "OPENROUTER_API_KEY")
        self._api_key = key
        # EXTRACTOR_MODEL is the bare first-party id; OpenRouter wants it namespaced.
        bare = (model or settings.EXTRACTOR_MODEL).split("/")[-1]
        self.model = model if model and "/" in model else f"anthropic/{bare}"
        self._base_url = base_url
        self._timeout = timeout if timeout is not None else settings.EXTRACTOR_TIMEOUT_S
        self._max_tokens = max_tokens
        self._client = client

    async def extract_citations(self, ai_output: str) -> CitationExtraction:
        from verifier.extraction.prompt import load_citation_prompt

        started = time.perf_counter()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": load_citation_prompt()},
                {"role": "user", "content": ai_output},
            ],
            "max_tokens": self._max_tokens,
            "temperature": 0,
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "sal-verifier",
                },
                json=body,
            )
            if response.status_code >= 400:
                raise FatalError(f"OpenRouter {response.status_code}: {response.text[:200]}")
            raw = _first_message_text(response.json())
        except Exception as exc:  # noqa: BLE001 - a provider outage is not a verdict
            return CitationExtraction(
                model=self.model,
                provider=self.provider,
                latency_ms=int((time.perf_counter() - started) * 1000),
                degraded=f"openrouter extraction failed: {exc}",
            )
        finally:
            if owns_client:
                await client.aclose()

        from verifier.providers.anthropic_llm import candidates_from

        elapsed = int((time.perf_counter() - started) * 1000)
        candidates = candidates_from(raw)
        if candidates is None:
            # Unparseable is NOT an empty list: reporting it as one would let a garbled
            # response fail an answer for citing nothing.
            return CitationExtraction(
                model=self.model,
                provider=self.provider,
                latency_ms=elapsed,
                parse_path=PARSE_PATH_UNPARSEABLE,
                degraded="openrouter extraction returned unparseable output",
            )
        return CitationExtraction(
            citations=candidates,
            model=self.model,
            provider=self.provider,
            latency_ms=elapsed,
        )
