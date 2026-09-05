"""Offline embedder: a hashed bag-of-words vectoriser.

NOT random vectors. Random vectors would make every similarity meaningless noise and
turn every threshold test into a test of the random seed -- the suite would pass while
proving nothing about the margin logic it claims to cover.

This produces genuine lexical similarity instead: text that shares vocabulary lands
close together, text that does not lands apart. That is enough for L3's contrastive
margin and L4's absolute bands to be exercised for real offline, and it degrades in the
same direction as a real embedder (paraphrase scores lower than verbatim), so a test
that passes here is testing the logic rather than the mock.

What it is NOT is a semantic model: it has no synonymy, so it will under-score a
correct paraphrase. Calibration numbers must never be derived from it -- thresholds are
keyed to ``EMBEDDINGS_MODEL`` for exactly this reason.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from verifier.providers.base import EmbeddingResult
from verifier.semantic.chunking import estimate_tokens
from verifier.settings import settings

_TOKEN = re.compile(r"[a-z0-9]+")

#: Function words carry no retrieval signal but appear in everything, so leaving them in
#: gives every pair of legal texts a high floor similarity -- an artificial anisotropy
#: that would collapse L3's margins and hide real failures behind a uniform 0.9.
_STOPWORDS = frozenset(
    """a an the and or but if of to in on at by for with from as is are was were be been
    being it its this that these those he she they them his her their we our you your i
    not no nor so than then there here which who whom whose what when where how why all
    any both each few more most other some such only own same too very can will just
    should now do does did doing have has had having would could may might must shall
    into over under again further once about against between during before after above
    below up down out off""".split()
)


def _tokenise(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


def _bucket(term: str, dim: int) -> tuple[int, float]:
    """Signed hashing: one hash picks the bucket, one bit picks the sign.

    The sign is the standard hashing-trick correction -- without it every collision adds
    energy in the same direction and unrelated documents drift together as the
    vocabulary grows.
    """
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


class MockEmbedder:
    """Deterministic, offline, and lexically meaningful."""

    def __init__(self, dim: int | None = None) -> None:
        # A distinct model name, never the real one: the embedding cache is keyed by
        # model, and a mock masquerading as voyage-law-2 would poison a shared cache
        # with vectors that are not comparable to anything.
        self.model = "mock-hashed-bow"
        self.dim = dim or settings.EMBEDDINGS_DIM

    async def embed(self, texts: list[str], *, input_type: str | None = None) -> EmbeddingResult:
        """``input_type`` is accepted and ignored.

        A bag of words has no notion of query-versus-document asymmetry, so honouring it
        would be theatre. It is still part of the signature because every caller must be
        written as though it matters -- it does for the real provider, and a call site
        that forgets it here would forget it there.
        """
        vectors = [self._vector(text) for text in texts]
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            tokens=sum(estimate_tokens(t) for t in texts),
            cache_misses=len(texts),
        )

    def _vector(self, text: str) -> list[float]:
        tokens = _tokenise(text)
        if not tokens:
            return [0.0] * self.dim
        # Unigrams plus bigrams: bigrams give the vector a little word-order sensitivity,
        # which is what separates "the defendant owed the plaintiff a duty" from the same
        # words rearranged. Weighted lower because they are sparser and noisier.
        counts = Counter(tokens)
        for first, second in zip(tokens, tokens[1:], strict=False):
            counts[f"{first}_{second}"] += 1

        vector = [0.0] * self.dim
        for term, count in counts.items():
            index, sign = _bucket(term, self.dim)
            # Sublinear term frequency: a word repeated 40 times is not 40x the evidence,
            # and without damping one repeated term dominates the whole vector.
            weight = (1.0 + math.log(count)) * (0.5 if "_" in term else 1.0)
            vector[index] += sign * weight

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]
