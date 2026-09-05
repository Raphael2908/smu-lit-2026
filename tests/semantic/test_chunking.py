"""Chunking. The load-bearing claim is that no chunk can overflow the context window."""

from __future__ import annotations

import pytest

from verifier.contracts.enums import ChunkKind
from verifier.semantic.chunking import (
    CHARS_PER_TOKEN,
    chunk_output_claims,
    chunk_source_document,
    estimate_tokens,
    locate_claim,
    split_sentences,
    window_claims,
)
from verifier.settings import settings

from .fixtures import (
    SPANDECK_PARAGRAPHS,
    StubSummariser,
    build_document,
    real_judgment_document,
    spandeck_document,
)


def test_estimate_tokens_is_a_rounded_up_char_ratio():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("a" * CHARS_PER_TOKEN) == 1
    assert estimate_tokens("a" * (CHARS_PER_TOKEN + 1)) == 2


def test_real_judgment_never_produces_an_oversized_chunk():
    """F9: a real judgment is ~21-28K tokens against a 16K context. This is the test
    that proves the mandatory-chunking claim rather than asserting it."""
    doc = real_judgment_document()
    assert len(doc.text) > 84_000, "corpus fixture must be a full-length judgment"
    assert estimate_tokens(doc.text) > settings.CHUNK_TARGET_TOKENS * 5

    chunks = chunk_source_document(doc)

    assert len(chunks) > 1
    for chunk in chunks:
        assert estimate_tokens(chunk.text) <= settings.CHUNK_TARGET_TOKENS
    # Nothing was dropped on the floor: every source paragraph is still reachable.
    joined = " ".join(c.text for c in chunks)
    assert doc.paragraphs[0].text[:60] in joined
    assert doc.paragraphs[-1].text[:60] in joined


def test_chunks_carry_paragraph_ranges_for_pinpoint_display():
    chunks = chunk_source_document(spandeck_document())
    assert chunks
    for chunk in chunks:
        assert chunk.paragraph_from is not None
        assert chunk.paragraph_to is not None
        assert chunk.paragraph_from <= chunk.paragraph_to
    assert chunks[0].paragraph_from == SPANDECK_PARAGRAPHS[0][0]


def test_a_heading_change_forces_a_new_chunk():
    """A chunk prefixed with a heading path that is true of only half of it is worse
    than a short chunk, so heading boundaries always cut."""
    chunks = chunk_source_document(spandeck_document())
    paths = [c.heading_path for c in chunks]
    assert len(set(paths)) == len(paths), "each heading path should own its own chunk"
    assert ("The duty of care", "Proximity") in paths


def test_an_oversized_single_paragraph_is_split_within_budget():
    sentence = "The defendant owed the plaintiff a duty of care in the circumstances. "
    doc = build_document(
        url="https://example.test/long",
        paragraphs=((1, ("Long",), sentence * 400),),
    )
    chunks = chunk_source_document(doc)
    assert len(chunks) > 1
    for chunk in chunks:
        assert estimate_tokens(chunk.text) <= settings.CHUNK_TARGET_TOKENS


def test_a_document_with_no_parsed_paragraphs_still_chunks():
    """A source fetched but not marked up must still be assessable, not silently zero."""
    doc = build_document(url="https://example.test/raw", paragraphs=())
    doc = doc.model_copy(update={"text": "One sentence. " * 3000, "paragraphs": ()})
    chunks = chunk_source_document(doc)
    assert chunks
    for chunk in chunks:
        assert estimate_tokens(chunk.text) <= settings.CHUNK_TARGET_TOKENS


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Spandeck Engineering v DSTA. The court held otherwise.", 2),
        # Abbreviations that genuinely occur in Singapore judgments must not split.
        ("See Tan v. Lim at [12]. That was wrong.", 2),
        ("ABC Pte. Ltd. was the appellant. It lost.", 2),
        ("See para. 14 of the judgment. Then see the rest.", 2),
    ],
)
def test_sentence_splitting_protects_legal_abbreviations(text, expected):
    assert len(split_sentences(text)) == expected


def test_window_claims_are_two_sentences_with_a_one_sentence_stride():
    text = "Alpha one here. Beta two here. Gamma three here. Delta four here."
    chunks = window_claims(text)
    assert [c.kind for c in chunks] == [ChunkKind.WINDOW] * len(chunks)
    assert chunks[0].text == "Alpha one here. Beta two here."
    assert chunks[1].text == "Beta two here. Gamma three here."
    assert chunks[-1].text.endswith("Delta four here.")
    # Spans are exact, because the windows are cut from the output itself.
    for chunk in chunks:
        assert chunk.span is not None
        assert text[chunk.span.start : chunk.span.end].startswith(chunk.text.split(".")[0])


def test_window_claims_handle_a_single_sentence_and_empty_text():
    assert window_claims("") == []
    single = window_claims("Only one sentence with no terminator")
    assert len(single) == 1
    assert single[0].span is not None


async def test_claim_splitting_prefers_the_summariser():
    output = "The duty of care is unitary. It has two stages."
    summariser = StubSummariser(claims=["The duty of care is unitary.", "It has two stages."])
    chunks = await chunk_output_claims(output, summariser=summariser)
    assert summariser.split_calls == 1
    assert [c.kind for c in chunks] == [ChunkKind.CLAIM, ChunkKind.CLAIM]
    assert [c.strategy for c in chunks] == ["claims", "claims"]
    assert all(c.span is not None for c in chunks)


@pytest.mark.parametrize(
    "summariser",
    [None, StubSummariser(claims=[]), StubSummariser(raises=True)],
    ids=["no-summariser", "empty-response", "provider-error"],
)
async def test_claim_splitting_falls_back_to_deterministic_windows(summariser):
    """The fallback carries no LLM call: it must run when the model tier is absent,
    unparseable, or down, without adding latency to an already-degraded path."""
    output = "Alpha one here. Beta two here. Gamma three here."
    chunks = await chunk_output_claims(output, summariser=summariser)
    assert chunks
    assert all(c.kind is ChunkKind.WINDOW for c in chunks)
    assert all(c.strategy == "window" for c in chunks)


async def test_claim_splitting_of_empty_output_is_empty():
    assert await chunk_output_claims("   ") == []


def test_locate_claim_finds_verbatim_and_lightly_reworded_claims():
    output = "In Spandeck the court held that a single test governs the duty of care."
    exact = locate_claim(output, "a single test governs the duty of care")
    assert exact is not None
    assert output[exact.start : exact.end] == "a single test governs the duty of care"

    fuzzy = locate_claim(output, "the court held that a single test governs a duty of care")
    assert fuzzy is not None


def test_locate_claim_returns_none_when_the_claim_is_not_in_the_output():
    """Deliberate: L3 attributes claims to citations by position, so a claim we cannot
    place must not be attributed to a citation by guesswork."""
    output = "In Spandeck the court held that a single test governs the duty of care."
    assert locate_claim(output, "The accused was charged under the Misuse of Drugs Act") is None
