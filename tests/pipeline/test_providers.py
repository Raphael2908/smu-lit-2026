"""Provider construction, the mocks, and the Anthropic judge's request shape."""

from __future__ import annotations

import json

import pytest

from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import FetchStrategy
from verifier.errors import FatalError, ProviderKeyMissing, RetryableError
from verifier.providers.anthropic_llm import AnthropicJudge, AnthropicSummariser, sentence_split
from verifier.providers.base import Judge, Summariser
from verifier.providers.mock.llm import MockJudge, MockSummariser
from verifier.providers.openrouter_llm import OpenRouterJudge

VERDICT = {
    "passed": True,
    "rubric": {
        "factual_faithfulness": 4,
        "contextual_accuracy": 4,
        "citation_integrity": 4,
        "responsiveness": 4,
    },
    "reasons": ["supported"],
}


class _Block:
    def __init__(self, text: str, kind: str = "text") -> None:
        self.type = kind
        self.text = text


class _Message:
    def __init__(self, *blocks) -> None:
        self.content = list(blocks)


class FakeAnthropicClient:
    """Stands in for ``anthropic.AsyncAnthropic``. Records every request."""

    def __init__(self, *replies: str, fail_output_config: bool = False) -> None:
        self.replies = list(replies) or [json.dumps(VERDICT)]
        self.calls: list[dict] = []
        self.fail_output_config = fail_output_config
        self.messages = self

    async def create(self, **kwargs):
        if self.fail_output_config and "output_config" in kwargs:
            raise TypeError("unexpected keyword argument 'output_config'")
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        # A thinking block before the text block: reading .text blindly would crash.
        return _Message(_Block("", "thinking"), _Block(reply))


# --- keys --------------------------------------------------------------------------


def test_a_blank_anthropic_key_raises_at_construction():
    with pytest.raises(ProviderKeyMissing, match="ANTHROPIC_API_KEY"):
        AnthropicJudge(api_key="")
    with pytest.raises(ProviderKeyMissing, match="ANTHROPIC_API_KEY"):
        AnthropicSummariser(api_key="  ")


def test_a_blank_openrouter_key_raises_at_construction():
    with pytest.raises(ProviderKeyMissing, match="OPENROUTER_API_KEY"):
        OpenRouterJudge(api_key="")


def test_the_anthropic_judge_uses_the_bare_model_id():
    """settings.JUDGE_MODEL is namespaced for OpenRouter; the first-party API is not.

    Asserted as a property rather than a literal: the configured model changes with
    deployment, but stripping the vendor prefix is the invariant. A test pinned to a
    model name breaks on every model change and tells you nothing about the bug it
    was written to catch.
    """
    from verifier.settings import settings

    judge = AnthropicJudge(client=FakeAnthropicClient())
    assert "/" not in judge.model
    assert judge.model == settings.JUDGE_MODEL.split("/")[-1]


def test_the_summariser_uses_the_configured_model_id():
    from verifier.settings import settings

    summariser = AnthropicSummariser(client=FakeAnthropicClient())
    assert summariser.model == settings.SUMMARISER_MODEL.split("/")[-1]
    assert "/" not in summariser.model


# --- request shape -----------------------------------------------------------------


async def test_the_anthropic_judge_asks_for_structured_output_and_no_temperature():
    client = FakeAnthropicClient()
    judge = AnthropicJudge(client=client)

    result = await judge.judge(system_prompt="the user-owned prompt", payload={})

    call = client.calls[0]
    from verifier.settings import settings

    assert call["model"] == settings.JUDGE_MODEL.split("/")[-1]
    assert call["system"] == "the user-owned prompt"
    assert call["output_config"]["format"]["type"] == "json_schema"
    # temperature was removed on Opus 5 / Sonnet 5 and returns a 400.
    assert "temperature" not in call
    assert result.passed is True
    assert result.provider == "anthropic"


async def test_an_sdk_without_output_config_falls_back_to_a_plain_request():
    client = FakeAnthropicClient(fail_output_config=True)
    judge = AnthropicJudge(client=client)

    result = await judge.judge(system_prompt="p", payload={})

    assert "output_config" not in client.calls[0]
    assert result.rubric is not None, "the parse ladder is the safety net either way"


async def test_the_anthropic_judge_repairs_once_then_gives_up():
    client = FakeAnthropicClient("not json", "still not json")
    judge = AnthropicJudge(client=client)

    result = await judge.judge(system_prompt="p", payload={})

    assert len(client.calls) == 2
    assert result.rubric is None
    assert result.passed is True, "fail open: an unreadable judge convicts nobody"


async def test_anthropic_errors_are_classified():
    class Failing(FakeAnthropicClient):
        def __init__(self, status: int) -> None:
            super().__init__()
            self.status = status

        async def create(self, **kwargs):
            exc = RuntimeError("upstream")
            exc.status_code = self.status  # type: ignore[attr-defined]
            raise exc

    judge = AnthropicJudge(client=Failing(503))
    with pytest.raises(RetryableError):
        await judge.judge(system_prompt="p", payload={})

    judge = AnthropicJudge(client=Failing(401))
    with pytest.raises(FatalError):
        await judge.judge(system_prompt="p", payload={})


async def test_split_claims_falls_back_to_a_sentence_split():
    """L3 needs claims more than it needs perfect claims."""

    class Broken(FakeAnthropicClient):
        async def create(self, **kwargs):
            raise RuntimeError("model unavailable")

    summariser = AnthropicSummariser(client=Broken())
    claims = await summariser.split_claims("First claim. Second claim. Third claim.")
    assert claims == ["First claim.", "Second claim.", "Third claim."]


async def test_split_claims_parses_a_json_array():
    client = FakeAnthropicClient('["one", "two"]')
    summariser = AnthropicSummariser(client=client)
    assert await summariser.split_claims("anything") == ["one", "two"]


# --- mocks -------------------------------------------------------------------------


def test_the_mocks_satisfy_the_provider_protocols():
    assert isinstance(MockJudge(), Judge)
    assert isinstance(MockSummariser(), Summariser)


async def test_the_mock_summariser_is_deterministic_and_offline():
    doc = SourceDocument(
        source_url="https://www.elitigation.sg/gd/s/2007_SGCA_37",
        domain="www.elitigation.sg",
        fetch_strategy=FetchStrategy.HTTP,
        exists=True,
        case_name="Spandeck v DSTA",
        court="SGCA",
        year=2007,
        text="The court held that a single test applies.",
    )
    summariser = MockSummariser()
    first = await summariser.summarise_document(doc)
    second = await summariser.summarise_document(doc)

    assert first == second
    assert "Spandeck v DSTA" in first
    assert summariser.calls == 2


async def test_the_mock_judge_records_what_it_was_asked():
    judge = MockJudge()
    await judge.judge(system_prompt="a prompt", payload={"run_id": "r"})

    assert judge.calls == 1
    assert judge.last_system_prompt == "a prompt"
    assert judge.last_payload == {"run_id": "r"}


async def test_the_mock_judge_can_simulate_a_repair_round_trip():
    judge = MockJudge(mode="garbage", repair_to="fenced")

    first = await judge.judge(system_prompt="p", payload={})
    second = await judge.judge(system_prompt="p", payload={})

    assert first.rubric is None
    assert second.rubric is not None
    assert second.parse_path == "fenced"


def test_sentence_split_keeps_terminators():
    assert sentence_split("A. B! C?") == ["A.", "B!", "C?"]
    assert sentence_split("") == []


# --- the citation extractor ---------------------------------------------------------


def _extractor(*replies: str, **kwargs):
    from verifier.providers.anthropic_llm import AnthropicCitationExtractor

    client = FakeAnthropicClient(*replies, **kwargs)
    return AnthropicCitationExtractor(client=client), client


async def test_the_extractor_pins_temperature_while_the_judge_still_sends_none():
    """The one deliberate exception in this module.

    Haiku 4.5 predates the 4.6 removal of sampling parameters, and pinning temperature
    to 0 is the cheapest defence against the drift that made L3 non-reproducible (F17):
    the same answer must yield the same citations twice. Sending it to the judge or the
    summariser -- both on 5-series models -- would return a 400.
    """
    extractor, client = _extractor('{"citations": [{"text": "[2007] SGCA 37"}]}')
    await extractor.extract_citations("The court held so in [2007] SGCA 37.")

    assert client.calls[0]["temperature"] == 0.0

    judge_client = FakeAnthropicClient()
    await AnthropicJudge(client=judge_client).judge(system_prompt="p", payload={})
    assert "temperature" not in judge_client.calls[0]


async def test_the_extractor_sends_neither_thinking_nor_effort():
    """Haiku 4.5 takes neither: it is a budget_tokens-era model, and effort errors."""
    extractor, client = _extractor('{"citations": []}')
    await extractor.extract_citations("An answer.")

    call = client.calls[0]
    assert "thinking" not in call
    assert "effort" not in call.get("output_config", {})


async def test_the_extractor_falls_back_to_a_plain_request_without_output_config():
    extractor, client = _extractor(
        '{"citations": [{"text": "[2007] SGCA 37"}]}', fail_output_config=True
    )
    result = await extractor.extract_citations("The court held so in [2007] SGCA 37.")

    assert "output_config" not in client.calls[0]
    assert [c.raw_text for c in result.citations] == ["[2007] SGCA 37"]
    assert result.degraded is None


async def test_a_fenced_response_still_parses_through_the_shared_ladder():
    extractor, _ = _extractor('Here you go:\n```json\n{"citations": [{"text": "x"}]}\n```')
    result = await extractor.extract_citations("An answer.")
    assert [c.raw_text for c in result.citations] == ["x"]


async def test_an_unparseable_response_degrades_rather_than_reporting_no_citations():
    """Unparseable is not an empty list. Reporting it as one would let a garbled
    response fail an answer for citing nothing."""
    extractor, _ = _extractor("I could not do that, sorry.")
    result = await extractor.extract_citations("An answer.")

    assert result.degraded is not None
    assert result.citations == ()


async def test_a_provider_error_degrades_rather_than_raising():
    """An exception out of L0 reaches L1a as an empty extraction, which is
    indistinguishable from an answer that cited nothing."""

    class Boom:
        messages = None

        async def create(self, **kwargs):
            raise RuntimeError("503")

    boom = Boom()
    boom.messages = boom
    from verifier.providers.anthropic_llm import AnthropicCitationExtractor

    result = await AnthropicCitationExtractor(client=boom).extract_citations("An answer.")
    assert result.degraded is not None
    assert result.citations == ()


async def test_the_openrouter_extractor_namespaces_the_bare_model_id():
    """EXTRACTOR_MODEL is the bare first-party id, which OpenRouter does not know.

    Getting this wrong fails at request time rather than at construction, which is the
    worst place to find it -- the same latent bug SUMMARISER_MODEL has in reverse.
    """
    from verifier.providers.openrouter_llm import OpenRouterCitationExtractor

    extractor = OpenRouterCitationExtractor(api_key="k")
    assert extractor.model == "anthropic/claude-haiku-4-5"
    assert OpenRouterCitationExtractor(api_key="k", model="x/y").model == "x/y"


async def test_the_openrouter_extractor_pins_temperature_and_degrades_on_error():
    import httpx

    from verifier.providers.openrouter_llm import OpenRouterCitationExtractor

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"citations": [{"text": "[2007] SGCA 37"}]}'}}]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = OpenRouterCitationExtractor(api_key="k", client=client)
    result = await extractor.extract_citations("The court held so in [2007] SGCA 37.")

    assert seen["temperature"] == 0
    assert [c.raw_text for c in result.citations] == ["[2007] SGCA 37"]

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    down = OpenRouterCitationExtractor(
        api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(boom))
    )
    degraded = await down.extract_citations("An answer.")
    assert degraded.degraded is not None
    assert degraded.citations == ()
