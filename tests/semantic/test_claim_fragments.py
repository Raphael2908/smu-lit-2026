"""Claims the splitter cut below the point where they can be checked.

``split_claims`` is a model call. The prompt asks for self-contained claims; a prompt
is a request, not a guarantee. It cut one sentence in half and L3 scored the halves:
the 58-character fragment "Policy considerations are applied only at the second stage"
scored 0.313 against a 0.35 floor, while the sentence it came from scores 0.649 in the
same configuration and clears the floor in every configuration (docs/03-findings.md
F18). The fragment does not say the second stage OF WHAT — it is a proposition the
answer never made, and L3 was asked to ground it.
"""

from __future__ import annotations

import pytest

from verifier.contracts.citations import Span
from verifier.contracts.enums import ChunkKind
from verifier.semantic.chunking import (
    RawChunk,
    chunk_output_claims,
    expand_fragments,
    locate_claim,
    sentence_containing,
)

SENTENCE = (
    "Policy considerations are applied only at the second stage, once a prima facie "
    "duty of care has been established."
)
ANSWER = f"{SENTENCE} Legal proximity encompasses physical, circumstantial and causal proximity."
FRAGMENT = "Policy considerations are applied only at the second stage"


def claim(text: str, answer: str = ANSWER) -> RawChunk:
    return RawChunk(
        ordinal=0,
        kind=ChunkKind.CLAIM,
        text=text,
        span=locate_claim(answer, text),
        strategy="claims",
    )


class Splitter:
    """Reproduces exactly what the live model returned for this answer."""

    model = "stub"

    def __init__(self, claims: list[str]) -> None:
        self._claims = claims

    async def summarise_document(self, doc) -> str:  # noqa: ANN001
        return ""

    async def split_claims(self, text: str) -> list[str]:
        return list(self._claims)


# --- the fix ------------------------------------------------------------------------


def test_a_fragment_is_restored_to_the_sentence_it_was_cut_from():
    out = expand_fragments(ANSWER, [claim(FRAGMENT)], min_chars=80)
    assert out[0].text == SENTENCE
    assert ANSWER[out[0].span.start : out[0].span.end] == SENTENCE


def test_two_fragments_of_one_sentence_collapse_to_one_claim():
    """Otherwise the same proposition is scored twice and the panel reports it twice."""
    halves = [claim(FRAGMENT), claim("once a prima facie duty of care has been established")]
    out = expand_fragments(ANSWER, halves, min_chars=80)
    assert len(out) == 1
    assert out[0].text == SENTENCE


def test_a_claim_above_the_floor_is_left_alone():
    """Expansion is a repair for a fragment, not a policy of preferring sentences."""
    long_claim = claim("Legal proximity encompasses physical, circumstantial and causal proximity")
    out = expand_fragments(ANSWER, [long_claim], min_chars=40)
    assert out[0].text == long_claim.text


def test_a_claim_that_cannot_be_located_is_not_expanded():
    """A claim the model paraphrased away from the text has no sentence to expand to,
    and inventing one would be worse than leaving it short."""
    orphan = RawChunk(ordinal=0, kind=ChunkKind.CLAIM, text="Short.", span=None, strategy="claims")
    out = expand_fragments(ANSWER, [orphan], min_chars=80)
    assert out[0].text == "Short."


def test_expansion_never_shrinks_a_claim():
    short_sentence = "It is so."
    text = f"{short_sentence} A much longer following sentence about proximity and policy."
    out = expand_fragments(text, [claim("It is so", text)], min_chars=80)
    assert len(out[0].text) >= len("It is so")


def test_a_runaway_sentence_is_not_expanded_into():
    """A 900-character sentence is not a better retrieval unit than the fragment."""
    long_sentence = "The court held that " + "the parties agreed on many terms " * 40 + "."
    out = expand_fragments(long_sentence, [claim("The court held", long_sentence)], min_chars=80)
    assert out[0].text == "The court held"


async def test_the_live_failure_no_longer_produces_a_fragment():
    """End to end through the splitter path, with the model's actual output."""
    claims = await chunk_output_claims(
        ANSWER,
        summariser=Splitter(
            [
                FRAGMENT,
                "Policy considerations are applied once a prima facie duty of care has "
                "been established",
                "Legal proximity encompasses physical proximity",
                "Legal proximity encompasses circumstantial proximity",
            ]
        ),
    )
    assert all(len(c.text) >= 60 for c in claims), [c.text for c in claims]
    assert any(c.text == SENTENCE for c in claims)


# --- locating a self-contained claim -------------------------------------------------


def test_a_restated_claim_still_locates_to_its_sentence():
    """Asking for self-contained claims makes them LESS verbatim, and partial_ratio
    penalises a claim for words the answer does not contain. Three of six claims fell
    below the threshold that way -- and a claim with no span is never attributed to a
    citation, so it is silently never scored. Better claims must not cost us the ability
    to place them."""
    answer = (
        "Singapore applies a single two-stage test: [2007] SGCA 37. "
        "The test is preceded by a preliminary requirement of factual foreseeability."
    )
    restated = (
        "The two-stage test for the imposition of a duty of care in negligence in "
        "Singapore is preceded by a preliminary requirement of factual foreseeability."
    )
    span = locate_claim(answer, restated)
    assert span is not None
    assert "factual foreseeability" in answer[span.start : span.end]


def test_an_unrelated_claim_does_not_locate():
    """Returning None is the safe answer: an unlocated claim is skipped and counted in
    retrieval coverage, whereas a wrong span scores it against a document it never
    referred to."""
    answer = "Singapore applies a single two-stage test for a duty of care: [2007] SGCA 37."
    assert locate_claim(answer, "Hearsay evidence is inadmissible under the Evidence Act.") is None


def test_sentence_containing_finds_the_enclosing_sentence():
    span = Span(start=0, end=len(FRAGMENT))
    found = sentence_containing(ANSWER, span)
    assert found is not None
    assert ANSWER[found.start : found.end] == SENTENCE


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_input_is_not_a_crash(text):
    assert locate_claim(text, "anything") is None
    assert expand_fragments(text, [], min_chars=80) == []
