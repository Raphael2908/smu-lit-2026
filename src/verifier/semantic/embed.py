"""Cache-through embedding.

This is the scalability story, so it is made measurable rather than asserted: every
call reports ``cache_hits`` and ``cache_misses``, those numbers ride out on
``LayerResult``, and the second query that touches a given judgment can be shown to
cost nothing. The cache is content-addressed on ``Chunk.embed_input_sha256``, which is
the hash of the exact string sent to the model, so it is impossible for a change in
prompt, summary or heading path to be served a stale vector.
"""

from __future__ import annotations

from verifier.contracts.documents import Chunk
from verifier.providers.base import Embedder, EmbeddingResult
from verifier.repos.base import EmbeddingRepo
from verifier.semantic.contextualise import sha256_text
from verifier.semantic.similarity import l2_normalise

#: Voyage's ``input_type``. Not cosmetic, and not interchangeable.
#:
#: A question is a short interrogative; a judgment paragraph is a long declarative
#: passage. Untagged, the two are embedded into different regions of the space and a
#: perfect answer scores low purely on length and register -- which means every
#: threshold derived from those scores is measuring prose style, not relevance.
#: Voyage's asymmetric prompts pull the query and its answer into the same retrieval
#: neighbourhood, which is the only reason a number like L4_PASS_AT can mean anything.
#:
#: The rule: whatever is being LOOKED UP WITH is a query; whatever is being SEARCHED is
#: a document. So questions and claims are queries, source chunks and answer chunks are
#: documents -- note that in L4 the answer is the corpus, not the query.
INPUT_TYPE_QUERY = "query"
INPUT_TYPE_DOCUMENT = "document"


class CachedEmbedder:
    """An :class:`Embedder` wrapper that reads through an :class:`EmbeddingRepo`."""

    def __init__(
        self,
        embedder: Embedder,
        repo: EmbeddingRepo | None = None,
        *,
        cache_namespace: str | None = None,
    ) -> None:
        self._embedder = embedder
        self._repo = repo
        self._cache_namespace = (cache_namespace or "").strip()

    @property
    def model(self) -> str:
        """The real model name, as reported on every ``EmbeddingResult``."""
        return self._embedder.model

    @property
    def cache_model(self) -> str:
        """The cache's identity: the model AND the regime its vectors were made under.

        Content-addressing on ``embed_input_sha256`` already stops a stale vector being
        READ after the prefix regime changes -- the hash simply misses. It does not stop
        the stale vector being SAMPLED, because ``sample_background`` selects on model
        alone. Without a namespace, flipping ``L3_CONTEXTUAL_PREFIX`` would leave L3
        contrasting freshly-embedded bare chunks against a background of prefixed ones,
        and a margin between two different embedding regimes measures the regimes rather
        than the claim.

        The direction of that error happens to be a false GREEN, which is exactly why it
        needs a mechanism rather than vigilance: nothing would have gone red to reveal it.

        Cost of the namespace is a one-time re-embed of each judgment under the new
        regime -- 43 calls for Spandeck -- which is the honest price of changing what
        gets embedded.
        """
        return f"{self.model}#{self._cache_namespace}" if self._cache_namespace else self.model

    @property
    def dim(self) -> int:
        return self._embedder.dim

    async def embed_texts(
        self,
        texts: list[str],
        *,
        input_type: str,
        document_id: str | None = None,
        cache: bool = True,
    ) -> EmbeddingResult:
        """Embed raw strings, hashing the string itself as the cache key."""
        return await self._embed(
            list(texts),
            [sha256_text(t) for t in texts],
            input_type=input_type,
            document_id=document_id,
            cache=cache,
        )

    async def embed_chunks(
        self,
        chunks: list[Chunk],
        *,
        input_type: str,
        document_id: str | None = None,
        cache: bool = True,
    ) -> EmbeddingResult:
        """Embed contextualised chunks, keyed on the hash of the full embed input."""
        return await self._embed(
            [c.embed_input for c in chunks],
            [c.embed_input_sha256 for c in chunks],
            input_type=input_type,
            document_id=document_id or next((c.document_id for c in chunks if c.document_id), None),
            cache=cache,
        )

    async def _embed(
        self,
        inputs: list[str],
        hashes: list[str],
        *,
        input_type: str,
        document_id: str | None,
        cache: bool = True,
    ) -> EmbeddingResult:
        if not inputs:
            return EmbeddingResult(vectors=[], model=self.model)

        # Deduplicate within the batch as well as against the cache: an AI output that
        # repeats a sentence must not be billed for it twice.
        unique_hashes: list[str] = []
        seen: set[str] = set()
        for h in hashes:
            if h not in seen:
                seen.add(h)
                unique_hashes.append(h)

        # ``cache=False`` is for the query side: a question, a claim or an answer chunk
        # is unique to one run, so caching it buys nothing -- and it would be actively
        # harmful, because L3's background pool is "every other cached vector" and a
        # claim that leaked into it would be compared against ITSELF. The margin would
        # then be strongly negative for a perfectly grounded claim. Only source
        # documents, which genuinely recur across runs, are cached.
        use_repo = self._repo is not None and cache

        cached: dict[str, list[float]] = {}
        if use_repo:
            cached = await self._repo.get_many(self.cache_model, unique_hashes)

        miss_hashes = [h for h in unique_hashes if h not in cached]
        by_hash = dict(zip(hashes, inputs, strict=True))
        tokens = 0
        fresh: dict[str, list[float]] = {}
        if miss_hashes:
            result = await self._embedder.embed(
                [by_hash[h] for h in miss_hashes], input_type=input_type
            )
            tokens = result.tokens
            if len(result.vectors) != len(miss_hashes):
                raise ValueError(
                    f"embedder returned {len(result.vectors)} vectors for {len(miss_hashes)} inputs"
                )
            # Normalise once, on write. Everything downstream is then a dot product, and
            # a vector fetched from the cache is guaranteed to be on the same scale as
            # one that was just computed -- otherwise a cache hit and a cache miss would
            # produce different similarity scores for identical text.
            fresh = {h: l2_normalise(v) for h, v in zip(miss_hashes, result.vectors, strict=True)}
            if use_repo:
                # ``document_id`` is what lets sample_background exclude this document's
                # own vectors from L3's contrastive baseline.
                await self._repo.put_many(self.cache_model, fresh, document_id=document_id)

        lookup = {**cached, **fresh}
        return EmbeddingResult(
            vectors=[lookup[h] for h in hashes],
            model=self.model,
            tokens=tokens,
            cache_hits=len(cached),
            cache_misses=len(miss_hashes),
        )

    async def sample_background(
        self, *, limit: int, exclude_document_id: str | None = None
    ) -> list[list[float]]:
        """Vectors from other cached judgments -- L3's contrastive baseline.

        Returns an empty list on a cold cache, which is a legitimate state on the very
        first run of a deployment. L3 must degrade to its absolute floor rather than
        crash or, worse, compute a margin against nothing and call it zero.
        """
        if self._repo is None:
            return []
        vectors = await self._repo.sample_background(
            self.cache_model, limit, exclude_document_id=exclude_document_id
        )
        return [l2_normalise(v) for v in vectors]
