"""The offline embedder must produce REAL lexical similarity.

If it returned random vectors the whole L3/L4 suite would be testing its own seed. These
tests pin the property the rest of the suite leans on: related legal text scores high,
unrelated legal text scores near zero, and the ordering is stable across processes.
"""

from __future__ import annotations

import math

import pytest

from verifier.providers.mock.embeddings import MockEmbedder
from verifier.semantic.similarity import cosine
from verifier.settings import settings

DUTY_OF_CARE = (
    "A single test determines the imposition of a duty of care in all claims arising "
    "out of negligence, irrespective of the type of damages claimed."
)
DUTY_PARAPHRASE = (
    "One universal test governs whether a duty of care is imposed in negligence claims, "
    "whatever damages are claimed."
)
DRUG_TRAFFICKING = (
    "The presumption of possession under section 18 of the Misuse of Drugs Act operates "
    "once the accused is proved to have had possession of the container."
)


async def _vector(embedder: MockEmbedder, text: str) -> list[float]:
    return (await embedder.embed([text], input_type="document")).vectors[0]


async def test_vectors_are_deterministic_and_the_configured_width():
    a = MockEmbedder()
    b = MockEmbedder()
    assert a.dim == settings.EMBEDDINGS_DIM
    first = await _vector(a, DUTY_OF_CARE)
    second = await _vector(b, DUTY_OF_CARE)
    assert first == second
    assert len(first) == settings.EMBEDDINGS_DIM


async def test_vectors_are_unit_length():
    embedder = MockEmbedder()
    vector = await _vector(embedder, DUTY_OF_CARE)
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


async def test_related_text_scores_far_above_unrelated_text():
    """The property that makes the margin and threshold tests meaningful rather than
    a test of the random seed."""
    embedder = MockEmbedder()
    anchor = await _vector(embedder, DUTY_OF_CARE)
    same = cosine(anchor, await _vector(embedder, DUTY_OF_CARE))
    near = cosine(anchor, await _vector(embedder, DUTY_PARAPHRASE))
    far = cosine(anchor, await _vector(embedder, DRUG_TRAFFICKING))

    assert math.isclose(same, 1.0, abs_tol=1e-9)
    assert near > 0.25
    assert far < 0.10
    assert same > near > far


async def test_the_model_name_is_never_the_real_one():
    """The embedding cache is keyed by model. A mock masquerading as voyage-law-2 would
    poison a shared cache with vectors that are comparable to nothing."""
    assert "voyage" not in MockEmbedder().model


@pytest.mark.parametrize("input_type", [None, "query", "document"])
async def test_input_type_is_accepted_even_though_a_bag_of_words_ignores_it(input_type):
    """Every call site must be written as though input_type matters -- it does for the
    real provider, and a call site that forgets it here would forget it there."""
    embedder = MockEmbedder()
    result = await embedder.embed([DUTY_OF_CARE], input_type=input_type)
    assert len(result.vectors) == 1


async def test_degenerate_input_yields_a_zero_vector_rather_than_an_error():
    embedder = MockEmbedder()
    result = await embedder.embed(["", "the and of", "..."], input_type="document")
    for vector in result.vectors:
        assert len(vector) == embedder.dim
        assert not any(vector)
    assert cosine(result.vectors[0], result.vectors[1]) == 0.0


async def test_word_order_is_not_entirely_ignored():
    """Bigrams give the vector enough order sensitivity to separate a sentence from its
    own shuffle, which pure unigram bag-of-words cannot do at all."""
    embedder = MockEmbedder()
    original = "the defendant owed the plaintiff a duty of care"
    shuffled = "the plaintiff owed the defendant a duty of care"
    assert cosine(await _vector(embedder, original), await _vector(embedder, shuffled)) < 1.0


async def test_reported_token_count_and_miss_count():
    embedder = MockEmbedder()
    result = await embedder.embed([DUTY_OF_CARE, DRUG_TRAFFICKING], input_type="document")
    assert result.cache_misses == 2
    assert result.tokens > 0
    assert result.model == embedder.model
