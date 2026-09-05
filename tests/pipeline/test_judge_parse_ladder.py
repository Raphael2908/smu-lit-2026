"""The judge parse ladder.

``response_format: json_schema`` with ``strict: true`` is requested, but enforcement
varies by underlying model, so a judge that only handles clean JSON is a judge that
fails on a bad day. Every rung is exercised here, offline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from verifier.errors import FatalError, ProviderKeyMissing, RetryableError
from verifier.providers.openrouter_llm import (
    PARSE_PATH_UNPARSEABLE,
    JudgeValidationError,
    OpenRouterJudge,
    coerce_judge_result,
    judge_json_schema,
    parse_judge_payload,
    validate_judge_object,
)

GOOD = {
    "passed": True,
    "rubric": {
        "factual_faithfulness": 4,
        "contextual_accuracy": 4,
        "citation_integrity": 3,
        "responsiveness": 4,
    },
    "reasons": ["every proposition is supported"],
}


def test_strict_json_is_the_first_rung():
    obj, path = parse_judge_payload(json.dumps(GOOD))
    assert path == "strict"
    assert obj == GOOD


def test_a_fenced_block_is_recovered():
    text = f"Here is my assessment.\n\n```json\n{json.dumps(GOOD)}\n```\n"
    obj, path = parse_judge_payload(text)
    assert path == "fenced"
    assert obj == GOOD


def test_a_bare_fence_is_recovered():
    text = f"```\n{json.dumps(GOOD)}\n```"
    obj, path = parse_judge_payload(text)
    assert path == "fenced"
    assert obj == GOOD


def test_trailing_prose_is_recovered_by_the_balanced_scan():
    """The commonest shape when a provider silently drops response_format."""
    text = (
        "After reviewing the passages my conclusion is: "
        + json.dumps(GOOD)
        + " Let me know if you would like more detail on any dimension."
    )
    obj, path = parse_judge_payload(text)
    assert path == "balanced"
    assert obj == GOOD


def test_the_balanced_scan_survives_braces_inside_strings():
    payload = dict(GOOD, reasons=['the answer quotes "{not json}" at [115]'])
    text = "Assessment follows. " + json.dumps(payload) + " Done."
    obj, path = parse_judge_payload(text)
    assert path == "balanced"
    assert obj["reasons"] == payload["reasons"]


def test_the_balanced_scan_survives_escaped_quotes():
    payload = dict(GOOD, reasons=['it says \\"held\\" but the case does not'])
    obj, _ = parse_judge_payload("prefix " + json.dumps(payload) + " suffix")
    assert obj is not None


def test_prose_with_no_json_is_unparseable():
    obj, path = parse_judge_payload("I am unable to produce structured output right now.")
    assert obj is None
    assert path == PARSE_PATH_UNPARSEABLE


def test_an_empty_response_is_unparseable():
    assert parse_judge_payload("") == (None, PARSE_PATH_UNPARSEABLE)
    assert parse_judge_payload("   \n ") == (None, PARSE_PATH_UNPARSEABLE)


# --- validation --------------------------------------------------------------------


def test_validation_accepts_a_well_formed_verdict():
    passed, rubric, reasons = validate_judge_object(GOOD)
    assert passed is True
    assert rubric.citation_integrity == 3
    assert reasons == ["every proposition is supported"]


def test_validation_is_tolerant_where_tolerance_is_safe():
    lenient = {
        "passed": "true",
        "rubric": {k: str(v) for k, v in GOOD["rubric"].items()},
        "reasons": "a single string reason",
    }
    passed, rubric, reasons = validate_judge_object(lenient)
    assert passed is True
    assert rubric.factual_faithfulness == 4
    assert reasons == ["a single string reason"]


@pytest.mark.parametrize(
    "obj, fragment",
    [
        ({"passed": True, "reasons": []}, "rubric"),
        ({"passed": True, "rubric": {"factual_faithfulness": 4}}, "missing"),
        ({"passed": True, "rubric": dict(GOOD["rubric"], responsiveness=9)}, "0-4"),
        ({"passed": True, "rubric": dict(GOOD["rubric"], responsiveness="x")}, "integer"),
        ({"rubric": GOOD["rubric"]}, "passed"),
    ],
)
def test_validation_rejects_a_rubric_it_cannot_trust(obj, fragment):
    """A score we cannot trust must become JUDGE_UNPARSEABLE, never a fabricated one."""
    with pytest.raises(JudgeValidationError, match=fragment):
        validate_judge_object(obj)


def test_coerce_reports_the_rung_that_won():
    result, error = coerce_judge_result(
        f"```json\n{json.dumps(GOOD)}\n```", model="m", provider="p"
    )
    assert error is None
    assert result is not None
    assert result.parse_path == "fenced"
    assert result.model == "m"


def test_coerce_returns_a_repair_message_it_can_send_back():
    result, error = coerce_judge_result("no json here", model="m", provider="p")
    assert result is None
    # Names both accepted formats, so the repair turn tells the model what to fix.
    assert "Correctness" in error
    assert "JSON object" in error


def test_the_response_schema_is_strict_shaped():
    schema = judge_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"passed", "rubric", "reasons"}
    assert schema["properties"]["rubric"]["properties"]["factual_faithfulness"]["maximum"] == 4


# --- the provider ------------------------------------------------------------------


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chat_response(text: str, *, cost: float = 0.0) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}}],
            "usage": {"cost": cost, "prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_a_blank_key_raises_at_construction():
    """Never fall back to a mock silently: a verifier that quietly stops verifying is
    worse than one that stops."""
    with pytest.raises(ProviderKeyMissing, match="OPENROUTER_API_KEY"):
        OpenRouterJudge(api_key="")
    with pytest.raises(ProviderKeyMissing):
        OpenRouterJudge(api_key="   ")


async def test_the_provider_recovers_a_fenced_response():
    def handler(_request: httpx.Request) -> httpx.Response:
        return _chat_response(f"```json\n{json.dumps(GOOD)}\n```", cost=0.012)

    judge = OpenRouterJudge(api_key="k", client=_client(handler))
    result = await judge.judge(system_prompt="prompt", payload={})

    assert result.parse_path == "fenced"
    assert result.passed is True
    assert result.cost_usd == pytest.approx(0.012)
    assert result.retries == 0


async def test_one_repair_retry_then_give_up():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["messages"][-1]["content"])
        return _chat_response("still not json, sorry")

    judge = OpenRouterJudge(api_key="k", client=_client(handler))
    result = await judge.judge(system_prompt="prompt", payload={})

    assert len(calls) == 2, "exactly ONE repair retry, never a loop"
    assert "could not be used" in calls[1], "the validation error is fed back"
    assert result.parse_path == PARSE_PATH_UNPARSEABLE
    assert result.rubric is None
    # Fail OPEN: a judge we could not read has not convicted anyone.
    assert result.passed is True
    assert result.retries == 1


async def test_the_repair_retry_can_succeed():
    responses = ["not json at all", json.dumps(GOOD)]

    def handler(_request: httpx.Request) -> httpx.Response:
        return _chat_response(responses.pop(0))

    judge = OpenRouterJudge(api_key="k", client=_client(handler))
    result = await judge.judge(system_prompt="prompt", payload={})

    assert result.rubric is not None
    assert result.retries == 1
    assert result.parse_path == "strict"


async def test_the_request_asks_for_a_strict_schema_without_relying_on_it():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer k"
        return _chat_response(json.dumps(GOOD))

    judge = OpenRouterJudge(api_key="k", model="anthropic/claude-opus-5", client=_client(handler))
    await judge.judge(system_prompt="the user-owned prompt", payload={})

    assert seen["model"] == "anthropic/claude-opus-5"
    assert seen["response_format"]["json_schema"]["strict"] is True
    assert seen["messages"][0] == {"role": "system", "content": "the user-owned prompt"}


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_upstream_failures_are_retryable(status):
    judge = OpenRouterJudge(
        api_key="k", client=_client(lambda _r: httpx.Response(status, text="nope"))
    )
    with pytest.raises(RetryableError):
        await judge.judge(system_prompt="p", payload={})


async def test_a_client_error_is_fatal():
    judge = OpenRouterJudge(
        api_key="k", client=_client(lambda _r: httpx.Response(400, text="bad model"))
    )
    with pytest.raises(FatalError):
        await judge.judge(system_prompt="p", payload={})


async def test_a_timeout_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    judge = OpenRouterJudge(api_key="k", client=_client(handler))
    with pytest.raises(RetryableError):
        await judge.judge(system_prompt="p", payload={})
