"""Quote extraction. The delimiter requirement is the point of these tests."""

from __future__ import annotations

import pytest

from verifier.extraction.quotes import extract_quotes

LONG = "the two-stage test comprising proximity and policy considerations applies here"
SHORT = "a short phrase"

DELIMITER_CASES = [
    (f"The court said “{LONG}”.", "“", LONG),
    (f'The court said "{LONG}".', '"', LONG),
    (f"The court said ‘{LONG}’.", "'", LONG),
    (f"The court said '{LONG}'.", "'", LONG),
]


@pytest.mark.parametrize(("text", "delimiter", "body"), DELIMITER_CASES)
def test_delimiters_are_recorded(text: str, delimiter: str, body: str) -> None:
    quotes = extract_quotes(text)
    assert len(quotes) == 1
    assert quotes[0].delimiter == delimiter
    assert quotes[0].text == body


def test_markdown_blockquote_is_a_quote() -> None:
    text = f"> {LONG}\n> and a second line of the same quoted block.\n\nOrdinary prose."
    quotes = extract_quotes(text)
    assert len(quotes) == 1
    assert quotes[0].delimiter == "blockquote"
    assert quotes[0].text.startswith("the two-stage test")
    assert ">" not in quotes[0].text  # markers are presentation, not quoted words


PARAPHRASES = [
    "The court observed that a single test governs every claim in negligence, whether or "
    "not the loss is purely economic in character.",
    "According to the Court of Appeal, proximity and policy are assessed in two stages "
    "with factual foreseeability as a threshold question.",
]


@pytest.mark.parametrize("text", PARAPHRASES)
def test_paraphrase_is_not_extracted_as_a_quote(text: str) -> None:
    """Nothing here carries a delimiter, so nothing here may reach L1.

    Measured under partial_ratio (docs/03-findings.md Part 3) an honest paraphrase
    scores 49.7 and an invented sentence 46.1 -- indistinguishable, and both below the
    75 FAIL threshold. Scoring un-delimited prose fails correct legal writing on a coin
    flip; whether such a claim is supported is L3's question.
    """
    assert extract_quotes(text) == []


def test_apostrophes_do_not_open_a_quote() -> None:
    """The single-quote pattern is the dangerous one: ' is also an apostrophe."""
    text = "Lim's client didn't object and the court's reasoning was accepted without demur."
    assert extract_quotes(text) == []


def test_short_spans_are_ignored() -> None:
    """Below the 40-char floor, partial_ratio matches almost anything in a 100kB
    judgment, so a short 'quote' is noise -- and acting on noise fails correct work."""
    assert extract_quotes(f'He called it "{SHORT}".') == []


def test_defined_terms_are_not_quotes() -> None:
    text = "Spandeck Engineering (S) Pte Ltd (“the appellant”) sued the respondent."
    assert extract_quotes(text) == []


def test_inline_quote_span_is_exact() -> None:
    text = f"The court said “{LONG}”."
    quote = extract_quotes(text)[0]
    assert text[quote.span.start : quote.span.end] == quote.text


def test_quote_inside_a_blockquote_is_reported_once() -> None:
    text = f'> The judge wrote "{LONG}" in terms.\n'
    quotes = extract_quotes(text)
    assert len(quotes) == 1
    assert quotes[0].delimiter == "blockquote"


def test_ordinals_are_positional() -> None:
    text = f"First “{LONG}” then second “{LONG} again”."
    quotes = extract_quotes(text)
    assert [q.ordinal for q in quotes] == [0, 1]
    assert quotes[0].span.start < quotes[1].span.start


def test_min_chars_is_overridable() -> None:
    assert extract_quotes(f'He said "{SHORT}".', min_chars=5)
