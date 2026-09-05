"""Attribution: which citation does a quote belong to, and to which paragraph."""

from __future__ import annotations

from verifier.contracts.enums import AttributionMethod
from verifier.extraction import extract

QUOTE = "a single test comprising proximity and policy considerations applies"
CASE = "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37"


def test_pinpoint_sets_the_paragraph_and_wins_over_proximity() -> None:
    """'at [115]' collapses the search space from an 84,000-character judgment to one
    paragraph, which is where most of L1's precision comes from."""
    result = extract(f"In {CASE} at [115], the court held “{QUOTE}”.")
    quote = result.quotes[0]
    assert quote.attribution_method is AttributionMethod.PINPOINT
    assert quote.pinpoint_paragraph == 115
    assert quote.attributed_cluster_ordinal == 0


def test_pinpoint_after_the_quote_also_counts() -> None:
    result = extract(f"The court held “{QUOTE}” ({CASE} at [115]).")
    assert result.quotes[0].pinpoint_paragraph == 115
    assert result.quotes[0].attribution_method is AttributionMethod.PINPOINT


def test_explicit_attribution_when_the_citation_shares_the_sentence() -> None:
    result = extract(f"{CASE} held that “{QUOTE}” in unambiguous terms.")
    quote = result.quotes[0]
    assert quote.attribution_method is AttributionMethod.EXPLICIT
    assert quote.attributed_cluster_ordinal == 0
    assert quote.pinpoint_paragraph is None


def test_proximity_attribution_within_the_same_paragraph() -> None:
    text = f"The authority is {CASE}. It has been followed since. The court held “{QUOTE}”."
    quote = extract(text).quotes[0]
    assert quote.attribution_method is AttributionMethod.PROXIMITY
    assert quote.attributed_cluster_ordinal == 0


def test_unattributed_quote_is_none_not_a_failure() -> None:
    """An unattributed quote is a quote we cannot check. Per the contract that is INFO;
    'cannot check' is never 'fabricated'."""
    quote = extract(f"Someone once wrote “{QUOTE}” without saying where from.").quotes[0]
    assert quote.attribution_method is AttributionMethod.NONE
    assert quote.attributed_cluster_ordinal is None


def test_attribution_does_not_cross_a_paragraph_boundary() -> None:
    text = f"The authority is {CASE}.\n\nSeparately, someone wrote “{QUOTE}”."
    quote = extract(text).quotes[0]
    assert quote.attribution_method is AttributionMethod.NONE


def test_pinpoint_does_not_leak_from_the_next_paragraph() -> None:
    """A confidently wrong paragraph number is worse than none: it points verification
    at text the quote was never taken from."""
    text = f"The court held “{QUOTE}”.\n\nA different case, at para 42, said otherwise."
    assert extract(text).quotes[0].pinpoint_paragraph is None


def test_citation_year_is_not_read_as_a_pinpoint() -> None:
    text = f"A decision reported at [2021] 1 SLR 55 held “{QUOTE}”."
    quote = extract(text).quotes[0]
    assert quote.pinpoint_paragraph is None
    assert quote.attribution_method is AttributionMethod.EXPLICIT


def test_pinpoint_picks_the_citation_nearest_the_pinpoint() -> None:
    text = f"Following [2008] SGCA 4, the position in {CASE} at [115] is that “{QUOTE}”."
    result = extract(text)
    quote = result.quotes[0]
    chosen = result.clusters[quote.attributed_cluster_ordinal or 0]
    assert quote.pinpoint_paragraph == 115
    assert any(m.raw_text == "[2007] SGCA 37" for m in chosen.members)
