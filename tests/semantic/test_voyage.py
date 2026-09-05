"""VoyageEmbedder. Exercised entirely offline against an injected fake client."""

from __future__ import annotations

import pytest

from verifier.errors import ProviderKeyMissing, RetryableError
from verifier.providers.voyage import MAX_BATCH, VoyageEmbedder


class FakeEmbeddingsObject:
    def __init__(self, embeddings: list[list[float]]) -> None:
        self.embeddings = embeddings
        self.total_tokens = len(embeddings) * 10


class FakeContextResult:
    def __init__(self, index: int, embeddings: list[list[float]]) -> None:
        self.index = index
        self.embeddings = embeddings


class FakeContextObject:
    def __init__(self, results: list[FakeContextResult]) -> None:
        self.results = results
        self.total_tokens = 5


class FakeVoyageClient:
    """Records every call. Never opens a socket."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.embed_batches: list[list[str]] = []
        self.embed_kwargs: list[dict] = []
        self.context_batches: list[list[list[str]]] = []
        self._raises = raises

    async def embed(self, texts, **kwargs):
        if self._raises:
            raise self._raises
        self.embed_batches.append(list(texts))
        self.embed_kwargs.append(kwargs)
        return FakeEmbeddingsObject([[float(len(t)), 0.0, 1.0] for t in texts])

    async def contextualized_embed(self, inputs, **kwargs):
        self.context_batches.append([list(doc) for doc in inputs])
        return FakeContextObject(
            [
                FakeContextResult(i, [[float(len(c)), 1.0] for c in doc])
                for i, doc in enumerate(inputs)
            ]
        )


def test_a_blank_key_raises_at_construction_and_never_falls_back():
    """A verifier that quietly stops verifying is worse than one that stops."""
    with pytest.raises(ProviderKeyMissing) as excinfo:
        VoyageEmbedder(api_key="")
    assert "VOYAGE_API_KEY" in str(excinfo.value)

    with pytest.raises(ProviderKeyMissing):
        VoyageEmbedder(api_key="   ")


def test_construction_with_a_key_does_not_import_or_contact_the_sdk():
    embedder = VoyageEmbedder(api_key="test-key", client=FakeVoyageClient())
    assert embedder.model
    assert embedder.dim > 0


async def test_input_type_is_forwarded_verbatim():
    client = FakeVoyageClient()
    embedder = VoyageEmbedder(api_key="k", model="voyage-law-2", client=client)

    await embedder.embed(["what is the test?"], input_type="query")
    await embedder.embed(["a long judgment passage"], input_type="document")

    assert [kw["input_type"] for kw in client.embed_kwargs] == ["query", "document"]
    assert {kw["model"] for kw in client.embed_kwargs} == {"voyage-law-2"}


async def test_requests_are_batched_at_the_provider_limit():
    client = FakeVoyageClient()
    embedder = VoyageEmbedder(api_key="k", client=client)
    texts = [f"passage {i}" for i in range(MAX_BATCH + 7)]

    result = await embedder.embed(texts, input_type="document")

    assert [len(batch) for batch in client.embed_batches] == [MAX_BATCH, 7]
    assert len(result.vectors) == len(texts)
    assert result.tokens == len(texts) * 10
    # Order is preserved across the batch boundary, which is what lets the caller line
    # vectors back up with chunks.
    assert result.vectors[MAX_BATCH][0] == float(len(texts[MAX_BATCH]))


async def test_empty_input_never_calls_the_provider():
    client = FakeVoyageClient()
    embedder = VoyageEmbedder(api_key="k", client=client)
    assert (await embedder.embed([], input_type="document")).vectors == []
    assert client.embed_batches == []


@pytest.mark.parametrize(
    "model, expected",
    [("voyage-law-2", False), ("voyage-context-4", True), ("voyage-3-large", False)],
)
def test_native_contextual_endpoint_is_selected_by_model(model, expected):
    embedder = VoyageEmbedder(api_key="k", model=model, client=FakeVoyageClient())
    assert embedder.uses_native_context is expected


async def test_contextualized_embed_keeps_documents_grouped():
    """Grouping is the unit of meaning: each chunk's vector attends to its neighbours,
    which is what makes the manual summary prefix unnecessary on this path."""
    client = FakeVoyageClient()
    embedder = VoyageEmbedder(api_key="k", model="voyage-context-4", client=client)

    out = await embedder.contextualized_embed(
        [["chunk one", "chunk two"], ["only chunk"]], input_type="document"
    )

    assert [len(doc) for doc in out] == [2, 1]
    assert client.context_batches == [[["chunk one", "chunk two"], ["only chunk"]]]
    assert await embedder.contextualized_embed([]) == []


@pytest.mark.parametrize("message", ["429 rate limit exceeded", "gateway timeout", "503 upstream"])
async def test_transient_vendor_errors_become_retryable(message):
    """The retry policy hinges on retryable-vs-fatal, so a vendor exception must never
    escape untranslated."""
    client = FakeVoyageClient(raises=RuntimeError(message))
    embedder = VoyageEmbedder(api_key="k", client=client)
    with pytest.raises(RetryableError):
        await embedder.embed(["x"], input_type="document")


async def test_non_transient_errors_are_not_swallowed():
    client = FakeVoyageClient(raises=ValueError("invalid model"))
    embedder = VoyageEmbedder(api_key="k", client=client)
    with pytest.raises(ValueError, match="invalid model"):
        await embedder.embed(["x"], input_type="document")
