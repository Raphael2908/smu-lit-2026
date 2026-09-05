"""Voyage embeddings.

Two paths, chosen by ``EMBEDDINGS_MODEL``:

* ``voyage-law-2`` (default) -- the ordinary embed endpoint. Chunks arrive already
  contextualised by :mod:`verifier.semantic.contextualise`, which prefixes the document
  summary and heading path by hand.
* ``voyage-context-4`` -- Voyage's native contextualised endpoint, which takes the
  whole document's chunk list in one call and lets each chunk's vector attend to its
  neighbours. That is strictly better than manual prefixing, so when it is selected the
  manual prefix is skipped rather than stacked on top of it.

The SDK is imported lazily inside the client property so the API tier never pays for a
vendor import it will not use, and so this module is importable in mock mode.
"""

from __future__ import annotations

from typing import Any

from verifier.errors import ProviderKeyMissing, RetryableError
from verifier.providers.base import EmbeddingResult
from verifier.settings import settings

#: Voyage accepts at most 128 inputs per embed call.
MAX_BATCH = 128

#: Models whose vectors come from the native contextualised endpoint.
_CONTEXT_MODEL_PREFIX = "voyage-context"


class VoyageEmbedder:
    """Batched Voyage embedder. Raises at construction if the key is missing."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        dim: int | None = None,
        client: Any | None = None,
    ) -> None:
        key = settings.VOYAGE_API_KEY if api_key is None else api_key
        # Fail at construction, not at first call, and never fall back to the mock. A
        # verifier that quietly stops verifying is worse than one that stops: the demo
        # keeps showing green ticks that mean nothing.
        if not (key or "").strip():
            raise ProviderKeyMissing("Voyage", "VOYAGE_API_KEY")
        self._api_key = key
        self.model = model or settings.EMBEDDINGS_MODEL
        self.dim = dim or settings.EMBEDDINGS_DIM
        self._client = client

    @property
    def uses_native_context(self) -> bool:
        """True when ``contextualized_embed`` should be used and manual prefixing skipped."""
        return self.model.startswith(_CONTEXT_MODEL_PREFIX)

    @property
    def client(self) -> Any:
        if self._client is None:
            import voyageai  # lazy: keeps the vendor SDK off the API tier's import path

            self._client = voyageai.AsyncClient(api_key=self._api_key, max_retries=2)
        return self._client

    async def embed(self, texts: list[str], *, input_type: str | None = None) -> EmbeddingResult:
        """Embed a flat list of strings.

        ``input_type`` is forwarded verbatim. It must be 'query' for questions and
        claims and 'document' for source and answer chunks: Voyage prepends different
        instructions for each, and mixing them up puts a question and its own answer in
        different regions of the space. Every threshold in settings.py assumes the
        tagging is correct.
        """
        if not texts:
            return EmbeddingResult(vectors=[], model=self.model)

        vectors: list[list[float]] = []
        tokens = 0
        for start in range(0, len(texts), MAX_BATCH):
            batch = texts[start : start + MAX_BATCH]
            response = await self._call(
                self.client.embed, batch, model=self.model, input_type=input_type
            )
            vectors.extend(response.embeddings)
            tokens += getattr(response, "total_tokens", 0) or 0
        return EmbeddingResult(
            vectors=vectors, model=self.model, tokens=tokens, cache_misses=len(texts)
        )

    async def contextualized_embed(
        self, documents: list[list[str]], *, input_type: str | None = None
    ) -> list[list[list[float]]]:
        """Embed whole documents chunk-by-chunk with cross-chunk attention.

        Takes a list of documents, each a list of chunk texts, and returns one vector
        per chunk grouped the same way. Only meaningful for ``voyage-context-*``: the
        model sees the neighbouring chunks, so "the appellant" resolves without us
        having to staple a summary onto the front of every chunk.

        Batching is by DOCUMENT here rather than by chunk, because the grouping is the
        unit of meaning -- splitting one judgment across two calls would defeat the
        point of the endpoint.
        """
        if not documents:
            return []
        out: list[list[list[float]]] = []
        for start in range(0, len(documents), MAX_BATCH):
            batch = documents[start : start + MAX_BATCH]
            response = await self._call(
                self.client.contextualized_embed, batch, model=self.model, input_type=input_type
            )
            for result in response.results:
                out.append([list(v) for v in result.embeddings])
        return out

    async def _call(self, fn: Any, payload: Any, **kwargs: Any) -> Any:
        try:
            return await fn(payload, **kwargs)
        except Exception as exc:  # noqa: BLE001 - normalised into the shared error hierarchy
            # Rate limits and upstream 5xx are transient; the retry policy hinges on
            # this classification, so a vendor exception must never escape untranslated.
            message = str(exc).lower()
            if any(hint in message for hint in ("rate limit", "429", "timeout", "503", "502")):
                raise RetryableError(f"Voyage: {exc}") from exc
            raise
