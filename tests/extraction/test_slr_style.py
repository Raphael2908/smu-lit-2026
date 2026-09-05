"""Conformance with the SAL *SLR Style Guide* (2021 ed) — the sponsor's house style.

Every example below is taken from the guide itself, with the paragraph cited. The
guide is the specification for what a citation in this corpus looks like, so a form it
prescribes that we fail to recognise is not a missed nicety: it is authority the answer
gets no credit for, and L1a fails an output that appears to cite nothing at all. Every
gap here is therefore a **false red** waiting to happen, which is the one error this
system must not make.
"""

from __future__ import annotations

import pytest

from verifier.contracts.enums import CitationType
from verifier.extraction import extract, extract_citations, extract_statutes


def citations_of(text: str, kind: CitationType) -> list[str]:
    return [c.raw_text for c in extract_citations(text) if c.citation_type is kind]


# --- 2-1.2.1 Singapore neutral citations -------------------------------------------


@pytest.mark.parametrize(
    "citation",
    [
        "[2003] SGCA 49",  # the guide's own example
        "[2003] SGHC 97",  # the guide's own example
        "[2020] SGHC(A) 4",
        "[2021] SGHC(I) 3",
        "[2023] SGCA(I) 1",
        "[2019] SGHCR 7",
        "[2022] SGHCF 12",
        "[2021] SGDC 44",
        "[2020] SGMC 9",
        "[2019] SGFC 61",
        # Added from the guide's court-designator table; previously unrecognised.
        "[2018] SGCT 1",
        "[2021] SGSCT 5",
        "[2020] SGYC 2",
    ],
)
def test_every_singapore_court_designator_is_recognised(citation):
    """Appendix 1B, para 2-1.2.1. A designator we miss is a citation that does not count."""
    assert citations_of(f"The court decided this in {citation}.", CitationType.NEUTRAL) == [
        citation
    ]


def test_the_guides_worked_neutral_citation_examples():
    text = (
        "Tan Kim Seng v Victor Adam Ibrahim [2003] SGCA 49 and "
        "Diva XL Pte Ltd v Lalasis Trading Pte Ltd [2003] SGHC 97 at [11]."
    )
    assert citations_of(text, CitationType.NEUTRAL) == ["[2003] SGCA 49", "[2003] SGHC 97"]


# --- 2-1.1.2 Year: brackets vs parentheses -----------------------------------------


@pytest.mark.parametrize(
    "citation",
    [
        "[2010] 1 SLR 1",  # year-organised series -> brackets
        "[2007] 4 SLR(R) 100",
        "[1932] AC 562",
        "[1985] 2 All ER 243",
        "(1992) 175 CLR 1",  # volume-organised series -> parentheses
        "(1908) 12 SSLR 120",
    ],
)
def test_both_bracket_styles_are_accepted(citation):
    """Para 2-1.1.2: brackets when the year is needed to find the case, parentheses
    when the series is published by volume number.

    Accepting only brackets silently dropped every volume-organised series.
    """
    assert citations_of(f"See {citation} on this point.", CitationType.REPORT) == [citation]


def test_a_bare_year_is_not_a_citation():
    """The year in one or the other bracket style stays mandatory: without it "Ch 1"
    and "AC 562" match ordinary prose, and a spurious citation is a spurious check."""
    assert citations_of("See Ch 1 of the report, at AC 562.", CitationType.REPORT) == []


# --- 2-1.1.5 Pinpoint citations ----------------------------------------------------


def test_a_bracketed_pinpoint_is_a_paragraph_and_a_bare_one_is_a_page():
    """Para 2-1.1.5 settles a question this codebase had already decided by reasoning:
    a paragraph pinpoint "should be in brackets, eg, '[2001] 3 SLR 10 at [16]'", while
    a page pinpoint is written bare, "without being preceded by 'p' or 'pp'".

    So the brackets are the entire signal. Reading "at 122" as paragraph 122 would send
    the quote check to a paragraph that does not exist and turn a verifiable quotation
    into an unverifiable one.
    """
    para = extract("The court said this in [2001] 3 SLR 10 at [16] plainly enough.")
    assert [q.pinpoint_paragraph for q in para.quotes] == []

    from verifier.extraction.attribution import _find_pinpoint
    from verifier.extraction.quotes import extract_quotes

    quoted = (
        'The court held at [16] that "the duty is one of reasonable care in all the circumstances".'
    )
    quotes = extract_quotes(quoted)
    assert quotes, "the quotation should be extracted"
    assert _find_pinpoint(quoted, quotes[0])[0] == 16

    paged = 'It was said at 122 that "the duty is one of reasonable care in all the circumstances".'
    assert _find_pinpoint(paged, extract_quotes(paged)[0]) is None


# --- 2-2.1 Citation of Singapore legislation ---------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Para 2-2.1.2.2(a): revised edition published before 1 March 2021.
        (
            "Supreme Court of Judicature Act (Cap 322, 2007 Rev Ed), First Schedule, para 19",
            "Supreme Court of Judicature Act (Cap 322, 2007 Rev Ed)",
        ),
        # Para 2-2.1.2.2(b): revised edition published on or after 1 March 2021.
        (
            "Supreme Court of Judicature Act 1969 (2020 Rev Ed), First Schedule, para 19",
            "Supreme Court of Judicature Act 1969 (2020 Rev Ed)",
        ),
        # Para 2-2.1.2.1: unrevised statute, 1965 to present.
        (
            "Administration of Justice (Protection) Act 2016 (Act 19 of 2016)",
            "Administration of Justice (Protection) Act 2016 (Act 19 of 2016)",
        ),
    ],
)
def test_the_guides_statute_citation_forms_parse_as_one_reference(text, expected):
    """A parenthesised qualifier in the short title ("(Protection)") used to defeat the
    match entirely, so an answer resting on that Act read as resting on nothing."""
    statutes = extract_statutes(text)
    assert [s.raw_text for s in statutes] == [expected]
    assert statutes[0].specific


def test_subsidiary_legislation_with_a_parenthesised_title():
    """Para 2-2.1.3.1's example."""
    statutes = extract_statutes("Active Mobility (Electronic Service System) Regulations 2019")
    assert statutes[0].act == "Active Mobility (Electronic Service System) Regulations"


def test_legislation_pinpoint_abbreviations_from_the_guide():
    """Para 1-3.2.2's table: s(s), reg(s), O, r(r), Pt(s), cl, Art."""
    assert extract_statutes("see s 5(1)(a) of that statute")[0].section == "5(1)(a)"
    assert extract_statutes("O 14 r 1 of the Rules")[0].section == "O 14 r 1"


def test_an_edition_marker_alone_is_not_authority():
    """ "2007 Rev Ed" names no legislation. Counting one would let an answer clear L1a
    by writing an edition marker."""
    assert extract_statutes("The 2007 Rev Ed superseded the earlier text.") == []


# --- 2-1.5 / 2-2.3 Subsequent references -------------------------------------------


def test_short_title_and_supra_references_count_as_authority():
    """Paras 2-1.1.1 and 2-1.5. SLR style cites in full once and refers back after, so
    a verifier that counts only full citations penalises the house style itself.

    This is the largest single source of false positives available to L1a.
    """
    text = (
        'The case of ANJ v ANK [2015] 4 SLR 1043 ("ANJ") stands for the proposition that a '
        "broad-brush approach applies to the division of matrimonial assets.\n\n"
        "As was discussed in ANJ ([1] supra), the court held that direct and indirect "
        "contributions are weighted separately.\n\n"
        "ANJ is therefore the governing authority. The test for indirect contributions is "
        "one of overall fairness rather than arithmetic precision.\n"
    )
    result = extract(text)
    assert len(result.clusters) == 1, "one case, cited once in full"
    assert result.propositions, "the later paragraphs do assert law"
    uncited = [p for p in result.propositions if not p.is_cited]
    assert uncited == [], f"SLR subsequent references should count as cited: {uncited}"


def test_an_undefined_capitalised_word_is_not_a_subsequent_reference():
    """Only a short title the output actually DEFINED is honoured. Otherwise any
    capitalised word could clear an assertion that nothing supports."""
    text = (
        "The Court of Appeal held that a broad-brush approach applies: "
        "ANJ v ANK [2015] 4 SLR 1043.\n\n"
        "It is well established that Parliament intended a different result."
    )
    result = extract(text)
    assert [p.is_cited for p in result.propositions][-1] is False
