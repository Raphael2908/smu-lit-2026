"""Citation extraction, case-name precision, and clustering."""

from __future__ import annotations

import pytest

from verifier.contracts.enums import CitationType
from verifier.extraction.citations import (
    cluster_citations,
    extract_citations,
    extract_clusters,
    search_phrase,
)

SPANDECK = (
    "The leading authority is Spandeck Engineering (S) Pte Ltd v Defence Science & "
    "Technology Agency [2007] 4 SLR(R) 100; [2007] SGCA 37."
)


def test_spandeck_triple_form_becomes_one_cluster() -> None:
    """Case name + report cite + neutral cite within ~80 chars is ONE reference.

    This is what rescues report-only citations (F7): the report citation cannot be
    resolved on its own, but it travels with a neutral citation that can be.
    """
    clusters = extract_clusters(SPANDECK)
    assert len(clusters) == 1
    types = {m.citation_type for m in clusters[0].members}
    assert types == {CitationType.CASE_NAME, CitationType.REPORT, CitationType.NEUTRAL}


def test_cluster_prefers_the_neutral_citation() -> None:
    preferred = extract_clusters(SPANDECK)[0].preferred
    assert preferred.citation_type is CitationType.NEUTRAL
    assert (preferred.court, preferred.year, preferred.number) == ("SGCA", 2007, 37)
    assert preferred.citation_key == "sgca:2007:37"


def test_cluster_stamps_the_case_name_onto_every_member() -> None:
    """The sibling case name is what makes the wrong-case cross-check possible: a real
    citation attached to the wrong parties is a distinct failure from a fake one."""
    cluster = extract_clusters(SPANDECK)[0]
    assert all(m.case_name and m.case_name.startswith("Spandeck") for m in cluster.members)
    assert search_phrase(cluster) is not None


def test_string_cite_of_two_cases_is_two_clusters() -> None:
    """Two neutral citations 2 characters apart are a string cite, not one reference.
    Merging them would drop a citation out of verification entirely."""
    clusters = extract_clusters("Compare [2007] SGCA 37; [2008] SGCA 4 on this point.")
    assert len(clusters) == 2


def test_clusters_do_not_span_a_sentence_boundary() -> None:
    text = "It follows from [2007] SGCA 37. See also [2020] SGHC(A) 4 for the later view."
    assert len(extract_clusters(text)) == 2


def test_far_apart_citations_are_separate_clusters() -> None:
    text = "[2007] SGCA 37" + " filler words " * 12 + "[2008] SGCA 4"
    assert len(cluster_citations(extract_citations(text))) == 2


# --- case-name precision ---------------------------------------------------

CASE_NAME_ACCEPTED = [
    (
        "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency",
        "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency",
    ),
    ("Tan Cheng Bock v AG said otherwise", "Tan Cheng Bock v AG"),
    ("Ng Kum Weng v Public Prosecutor", "Ng Kum Weng v Public Prosecutor"),
    ("In Tan Ah Kow v Lim Bee Choo the court", "Tan Ah Kow v Lim Bee Choo"),
    (
        "Ocean Front Pte Ltd vs RSP Architects Planners",
        "Ocean Front Pte Ltd vs RSP Architects Planners",
    ),
]


@pytest.mark.parametrize(("text", "expected"), CASE_NAME_ACCEPTED)
def test_case_name_accepted(text: str, expected: str) -> None:
    names = [
        c.raw_text for c in extract_citations(text) if c.citation_type is CitationType.CASE_NAME
    ]
    assert names == [expected]


CASE_NAME_REJECTED = [
    "The Court v His Honour",  # both sides are stopwords
    "The court held v the judge disagreed",  # lowercase right side
    "Singapore v Malaysia",  # one token per side
    "Donoghue v Stevenson",  # one token per side; rescued by its report cite instead
    "He v She",
    "V K Rajah JA delivered the judgment",  # uppercase V is an initial, not a separator
    "the plaintiff v the defendant",
    "Chan Sek Keong CJ; Andrew Phang Boon Leong JA",  # no separator at all
]


@pytest.mark.parametrize("text", CASE_NAME_REJECTED)
def test_case_name_rejected(text: str) -> None:
    """A bad case-name parse produces a search phrase that legitimately returns zero
    hits -- and zero hits is our fabrication signal. Every one of these, if accepted,
    is a false fabrication claim against text that is not even a citation."""
    names = [c for c in extract_citations(text) if c.citation_type is CitationType.CASE_NAME]
    assert names == []


def test_case_name_does_not_swallow_the_next_sentence() -> None:
    text = "Tan Ah Kow Holdings v Lim Bee Choo Enterprises The Court then considered costs."
    name = next(c for c in extract_citations(text) if c.citation_type is CitationType.CASE_NAME)
    assert name.raw_text == "Tan Ah Kow Holdings v Lim Bee Choo Enterprises"


def test_spans_are_exact_offsets_into_the_output() -> None:
    """The UI highlights by span, so an off-by-one is a visibly wrong accusation."""
    for citation in extract_citations(SPANDECK):
        assert SPANDECK[citation.span.start : citation.span.end] == citation.raw_text


def test_ordinals_are_stable_and_positional() -> None:
    citations = extract_citations(SPANDECK)
    assert [c.ordinal for c in citations] == list(range(len(citations)))
    assert [c.span.start for c in citations] == sorted(c.span.start for c in citations)


def test_report_only_citation_yields_a_report_cluster() -> None:
    cluster = extract_clusters("The rule appears at [2021] 1 SLR 55 alone.")[0]
    assert cluster.preferred.citation_type is CitationType.REPORT
    assert search_phrase(cluster) is None


def test_url_is_extracted_and_not_mistaken_for_a_case_name() -> None:
    citations = extract_citations("See https://www.elitigation.sg/gd/s/2007_SGCA_37 for the text.")
    assert [c.citation_type for c in citations] == [CitationType.URL]
