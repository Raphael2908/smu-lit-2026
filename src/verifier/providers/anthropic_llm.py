"""Anthropic providers: the document summariser, and the judge as an alternative to
OpenRouter.

The SDK is imported lazily inside each call path so the API tier never pays for a
vendor SDK it will not use, and so ``PROVIDER_MODE=mock`` never needs it installed.

Model IDs are exact and carry no date suffix: ``claude-sonnet-5``, ``claude-opus-5``.

Structured output is requested via ``output_config={"format": ...}`` -- the current
Messages API shape (the older top-level ``output_format`` parameter is deprecated).
Support is probed at call time and falls back to a plain request, because a judge that
breaks on an SDK or model that has not shipped the feature is a judge that is down.
Either way the parse ladder from ``openrouter_llm`` is the safety net: structured
output is an optimisation, the ladder is the contract.

Note what is NOT sent: ``temperature``. It was removed on Sonnet 5 / Opus 5 and passing
it returns a 400.
"""

from __future__ import annotations

import re
import time
from typing import Any

from verifier.contracts.documents import SourceDocument
from verifier.errors import FatalError, ProviderKeyMissing, RetryableError
from verifier.providers.base import JudgeResult
from verifier.providers.openrouter_llm import (
    coerce_judge_result,
    judge_json_schema,
    unparseable_result,
)

__all__ = ["AnthropicJudge", "AnthropicSummariser"]

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")

_JUDGE_USER_TURN = (
    "Assess the answer against the passages above and return ONLY the JSON object "
    "described. No preamble, no code fence, no commentary."
)


def _require_key(api_key: str | None) -> str:
    from verifier.settings import settings

    key = api_key if api_key is not None else settings.ANTHROPIC_API_KEY
    if not key or not key.strip():
        raise ProviderKeyMissing("Anthropic", "ANTHROPIC_API_KEY")
    return key


def _client(api_key: str) -> Any:
    """Build an async client. Imported here so the SDK is only loaded when used."""
    import anthropic

    return anthropic.AsyncAnthropic(api_key=api_key)


def _text_of(message: Any) -> str:
    """Concatenate the text blocks of a Messages response.

    ``content`` is a list of blocks (text, thinking, tool_use, ...); check ``.type``
    before reading ``.text`` or a thinking block will crash the caller.
    """
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


def _map_error(exc: Exception) -> Exception:
    """Retryable vs fatal, so the task retry policy has something to act on."""
    status = getattr(exc, "status_code", None)
    if status is None:
        return RetryableError(f"Anthropic transport error: {exc}")
    if status == 429 or status >= 500:
        return RetryableError(f"Anthropic returned {status}: {exc}")
    return FatalError(f"Anthropic returned {status}: {exc}")


async def _create(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    schema: dict[str, Any] | None = None,
) -> Any:
    """One request, with a graceful degrade if ``output_config`` is unsupported.

    An SDK too old for ``output_config`` raises TypeError locally; a model that does not
    support it returns a 400. Both mean "ask again without it" rather than "fail the
    run" -- the parse ladder handles unconstrained output perfectly well.
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if schema is not None:
        try:
            return await client.messages.create(
                **kwargs,
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except TypeError:
            pass
        except Exception as exc:  # noqa: BLE001 - narrowed below
            if getattr(exc, "status_code", None) != 400:
                raise _map_error(exc) from exc
    try:
        return await client.messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise _map_error(exc) from exc


class AnthropicSummariser:
    """Document summaries for the embedding input, and claim splitting for L3.

    The summary is prepended to every chunk's ``embed_input``, which is why its hash is
    the cache key: changing this prompt correctly invalidates the embedding cache, and
    ``settings.SUMMARY_PROMPT_VERSION`` is what makes that visible.
    """

    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        client: Any | None = None,
    ) -> None:
        from verifier.settings import settings

        self._api_key = _require_key(api_key) if client is None else (api_key or "")
        # Same normalisation as the judge: settings.SUMMARISER_MODEL is namespaced for
        # OpenRouter ("anthropic/claude-haiku-4.5") and the first-party API wants the
        # bare id. Without this, switching SUMMARISER_PROVIDER to anthropic sends an
        # unknown model id and fails at request time rather than at construction.
        self.model = model or settings.SUMMARISER_MODEL.split("/")[-1] or "claude-haiku-4-5"
        self._client = client
        self._max_tokens = max(256, settings.SUMMARY_MAX_TOKENS * 4)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = _client(self._api_key)
        return self._client

    async def summarise_document(self, doc: SourceDocument) -> str:
        from verifier.settings import settings

        header = " / ".join(
            part
            for part in (doc.neutral_citation, doc.case_name, doc.court, str(doc.year or ""))
            if part
        )
        # Bounded input: the summary exists to give a chunk its context, not to read
        # the whole judgment again. A judgment is ~21K tokens (F9).
        body = doc.text[:20000]
        system = (
            "You summarise Singapore judgments for a retrieval index. Write at most "
            f"{settings.SUMMARY_MAX_TOKENS} tokens of plain prose covering: the parties "
            "and posture, the legal issues decided, the holding, and the test or "
            "principle the case is cited for. State only what the text supports. No "
            "preamble, no headings, no markdown."
        )
        message = await _create(
            self._get_client(),
            model=self.model,
            system=system,
            user=f"{header}\n\n{body}",
            max_tokens=self._max_tokens,
        )
        return _text_of(message).strip()

    async def split_claims(self, text: str) -> list[str]:
        """Split an answer into checkable legal claims, one proposition each.

        Falls back to a sentence split when the model returns something unusable --
        L3 needs claims more than it needs perfect claims.
        """
        system = (
            "Split the text into standalone factual or legal claims, one proposition "
            "each, preserving the original wording as closely as possible. Drop "
            "hedges, pleasantries and disclaimers. Return ONLY a JSON array of strings."
        )
        try:
            message = await _create(
                self._get_client(),
                model=self.model,
                system=system,
                user=text,
                max_tokens=2048,
                schema={"type": "array", "items": {"type": "string"}},
            )
        except Exception:  # noqa: BLE001 - degrade rather than fail the layer
            return sentence_split(text)
        return _claims_from(_text_of(message)) or sentence_split(text)


def sentence_split(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(text.strip()) if s.strip()]


def _claims_from(raw: str) -> list[str]:
    import json

    from verifier.providers.openrouter_llm import _try_balanced  # noqa: PLC0415

    for candidate in (raw, raw[raw.find("[") : raw.rfind("]") + 1] if "[" in raw else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    obj = _try_balanced(raw)
    if isinstance(obj, dict):
        for value in obj.values():
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
    return []


class AnthropicJudge:
    """The same ``Judge`` protocol as ``OpenRouterJudge``, against the first-party API.

    Orchestration never learns which one it got: swapping vendors is a factory line.
    """

    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        client: Any | None = None,
    ) -> None:
        from verifier.settings import settings

        self._api_key = _require_key(api_key) if client is None else (api_key or "")
        # settings.JUDGE_MODEL is namespaced for OpenRouter ("anthropic/claude-opus-5");
        # the first-party API wants the bare id.
        self.model = model or settings.JUDGE_MODEL.split("/")[-1] or "claude-opus-5"
        self._max_tokens = max_tokens
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = _client(self._api_key)
        return self._client

    async def judge(self, *, system_prompt: str, payload: dict) -> JudgeResult:
        started = time.perf_counter()
        client = self._get_client()
        message = await _create(
            client,
            model=self.model,
            system=system_prompt,
            user=_JUDGE_USER_TURN,
            max_tokens=self._max_tokens,
            schema=judge_json_schema(),
        )
        text = _text_of(message)
        elapsed = int((time.perf_counter() - started) * 1000)
        result, error = coerce_judge_result(
            text, model=self.model, provider=self.provider, latency_ms=elapsed
        )
        if result is not None:
            return result

        # ONE repair retry with the validation error appended, then give up.
        repair = (
            f"{_JUDGE_USER_TURN}\n\nYour previous response could not be used: {error}. "
            "Return the JSON object only."
        )
        retry_message = await _create(
            client,
            model=self.model,
            system=system_prompt,
            user=repair,
            max_tokens=self._max_tokens,
            schema=judge_json_schema(),
        )
        retry_text = _text_of(retry_message)
        elapsed = int((time.perf_counter() - started) * 1000)
        retried, _ = coerce_judge_result(
            retry_text,
            model=self.model,
            provider=self.provider,
            latency_ms=elapsed,
            retries=1,
        )
        if retried is not None:
            return retried
        return unparseable_result(
            retry_text or text,
            model=self.model,
            provider=self.provider,
            latency_ms=elapsed,
            retries=1,
        )
