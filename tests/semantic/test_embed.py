"""Cache-through embedding. The scalability claim, made measurable."""

from __future__ import annotations

import math

import pytest

from verifier.providers.mock.embeddings import MockEmbedder
from verifier.repos.memory import InMemoryEmbeddingRepo
from verifier.semantic.chunking import chunk_source_document
from verifier.semantic.contextualise import build_chunks, sha256_text
from verifier.semantic.embed import INPUT_TYPE_DOCUMENT, INPUT_TYPE_QUERY, CachedEmbedder

from .fixtures import CountingEmbedder, spandeck_document

TEXTS = [
    "A single test governs the imposition of a duty of care.",
    "Legal proximity encompasses physical, circumstantial and causal proximity.",
]


def _embedder() -> tuple[CountingEmbedder, InMemoryEmbeddingRepo, CachedEmbedder]:
    counting = CountingEmbedder(MockEmbedder())
    repo = InMemoryEmbeddingRepo()
    return counting, repo, CachedEmbedder(counting, repo)


async def test_second_call_for_the_same_text_makes_zero_provider_calls():
    counting, _repo, cached = _embedder()

    first = await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)
    assert counting.calls == 1
    assert counting.texts == 2
    assert (first.cache_hits, first.cache_misses) == (0, 2)

    second = await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)

    assert counting.calls == 1, "a cache hit must not reach the provider at all"
    assert counting.texts == 2
    assert (second.cache_hits, second.cache_misses) == (2, 0)
    assert second.vectors == first.vectors


async def test_a_partial_hit_only_embeds_the_misses():
    counting, _repo, cached = _embedder()
    await cached.embed_texts(TEXTS[:1], input_type=INPUT_TYPE_DOCUMENT)

    result = await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)

    assert (result.cache_hits, result.cache_misses) == (1, 1)
    assert counting.texts == 2, "only the miss is sent on the second call"


async def test_duplicates_within_one_batch_are_embedded_once():
    """An AI output that repeats a sentence must not be billed for it twice."""
    counting, _repo, cached = _embedder()

    result = await cached.embed_texts(
        [TEXTS[0], TEXTS[1], TEXTS[0]], input_type=INPUT_TYPE_DOCUMENT
    )

    assert counting.texts == 2
    assert len(result.vectors) == 3
    assert result.vectors[0] == result.vectors[2]


async def test_vectors_are_unit_length_whether_fresh_or_cached():
    """Normalising on write is what makes a cache hit and a cache miss score the same."""
    _counting, _repo, cached = _embedder()
    fresh = await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)
    hit = await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)

    for vectors in (fresh.vectors, hit.vectors):
        for vector in vectors:
            assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


async def test_input_type_is_forwarded_to_the_provider():
    """The query/document asymmetry is load-bearing; the wrapper must never drop it."""
    counting, _repo, cached = _embedder()
    await cached.embed_texts(["a question?"], input_type=INPUT_TYPE_QUERY)
    await cached.embed_texts(["a passage."], input_type=INPUT_TYPE_DOCUMENT)
    assert counting.input_types == [INPUT_TYPE_QUERY, INPUT_TYPE_DOCUMENT]


async def test_cache_false_never_touches_the_repo():
    """Query-side vectors are per-run and must stay out of the shared store: a cached
    claim would end up in a later run's L3 background pool, matching itself."""
    counting, repo, cached = _embedder()

    await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_QUERY, cache=False)
    await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_QUERY, cache=False)

    assert counting.calls == 2, "nothing was cached, so the second call re-embeds"
    hashes = [sha256_text(t) for t in TEXTS]
    assert await repo.get_many(cached.model, hashes) == {}
    assert await cached.sample_background(limit=10) == []


async def test_chunks_are_keyed_on_the_embed_input_hash():
    counting, repo, cached = _embedder()
    raws = chunk_source_document(spandeck_document())

    with_summary = build_chunks(raws, summary="Summary A.", document_id="doc-1")
    await cached.embed_chunks(with_summary, input_type=INPUT_TYPE_DOCUMENT)
    calls_after_first = counting.calls

    # Same text, new summary -> new hash -> genuine miss. This is the prompt-change
    # invalidation working end to end.
    with_new_summary = build_chunks(raws, summary="Summary B.", document_id="doc-1")
    result = await cached.embed_chunks(with_new_summary, input_type=INPUT_TYPE_DOCUMENT)

    assert counting.calls == calls_after_first + 1
    assert result.cache_hits == 0
    assert result.cache_misses == len(raws)

    stored = await repo.get_many(cached.model, [c.embed_input_sha256 for c in with_summary])
    assert len(stored) == len(with_summary)


async def test_background_sampling_excludes_the_document_itself():
    """If a document appeared in its own background sample, the claim's best match would
    sit on both sides of the margin and cancel itself out."""
    counting, repo, cached = _embedder()
    doc = spandeck_document()
    chunks = build_chunks(chunk_source_document(doc), document_id="doc-spandeck")
    await cached.embed_chunks(chunks, input_type=INPUT_TYPE_DOCUMENT, document_id="doc-spandeck")

    assert await cached.sample_background(limit=50, exclude_document_id="doc-spandeck") == []
    assert len(await cached.sample_background(limit=50, exclude_document_id="other")) == len(chunks)
    assert counting.calls == 1


async def test_no_repo_means_no_cache_and_no_background():
    counting = CountingEmbedder(MockEmbedder())
    cached = CachedEmbedder(counting, None)
    await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)
    result = await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)
    assert counting.calls == 2
    assert (result.cache_hits, result.cache_misses) == (0, 2)
    assert await cached.sample_background(limit=5) == []


async def test_empty_input_is_a_no_op():
    counting, _repo, cached = _embedder()
    result = await cached.embed_texts([], input_type=INPUT_TYPE_QUERY)
    assert result.vectors == []
    assert counting.calls == 0


async def test_a_provider_returning_the_wrong_vector_count_is_an_error():
    class Broken:
        model = "broken"
        dim = 4

        async def embed(self, texts, *, input_type=None):
            from verifier.providers.base import EmbeddingResult

            return EmbeddingResult(vectors=[[1.0, 0.0, 0.0, 0.0]], model=self.model)

    cached = CachedEmbedder(Broken(), InMemoryEmbeddingRepo())
    with pytest.raises(ValueError, match="vectors"):
        await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)


class BrokenRepo:
    """Every method raises, the way a Postgres repo can when the database is not there."""

    def __init__(self) -> None:
        self.writes = 0

    async def get_many(self, model, input_hashes):
        raise RuntimeError("connection refused")

    async def put_many(self, model, vectors, document_id=None):
        self.writes += 1
        raise RuntimeError("connection refused")

    async def sample_background(self, model, limit, exclude_document_id=None):
        raise RuntimeError("connection refused")


async def test_a_broken_cache_degrades_to_a_miss_rather_than_failing_the_layer():
    """A cache is not a verdict.

    The repo used to be a process-local dict that could not fail. It is now whatever
    ``get_repos()`` holds, which in production is Postgres over a network, so every call
    acquired a new failure mode. We would rather re-embed a judgment than fail a citation
    over a database blip -- the rule ``_persist_resolutions`` already states.
    """
    counting = CountingEmbedder(MockEmbedder())
    repo = BrokenRepo()
    cached = CachedEmbedder(counting, repo)

    result = await cached.embed_texts(TEXTS, input_type=INPUT_TYPE_DOCUMENT)

    assert len(result.vectors) == len(TEXTS), "the vectors are correct despite the outage"
    assert all(math.isclose(sum(v * v for v in vec), 1.0, rel_tol=1e-6) for vec in result.vectors)
    assert (result.cache_hits, result.cache_misses) == (0, 2)
    assert repo.writes == 1, "the write was attempted, and its failure swallowed"


async def test_a_broken_cache_yields_an_empty_background_not_an_exception():
    """L3 must fall back to its absolute floor, which an empty pool already means."""
    cached = CachedEmbedder(CountingEmbedder(MockEmbedder()), BrokenRepo())

    assert await cached.sample_background(limit=8, exclude_document_id="doc-1") == []


async def test_the_zero_argument_layer_default_takes_its_repo_from_the_bundle():
    """The composition root must actually swap the repos, which is what was never wired.

    ``registry.build_layer`` builds L3 and L4 with no arguments, so the default is the
    only repo they will ever get. It returned a process-local dict while
    ``build_pg_repos()`` constructed a Postgres repo nothing read -- two caches side by
    side, so ``text_embeddings`` stayed empty and every run re-embedded the judgment
    (F25). Pinning it against ``set_repos`` proves the bundle is the source of truth
    without needing a database.
    """
    from verifier.repos.pg import Repos, get_repos, set_repos
    from verifier.semantic.defaults import default_embedding_repo, reset_default_repos

    sentinel = InMemoryEmbeddingRepo()
    bundle = get_repos()
    reset_default_repos()
    set_repos(
        Repos(
            documents=bundle.documents,
            resolutions=bundle.resolutions,
            embeddings=sentinel,
            runs=bundle.runs,
            lists=bundle.lists,
        )
    )
    try:
        assert default_embedding_repo() is sentinel
    finally:
        set_repos(None)
        reset_default_repos()
