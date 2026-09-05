"""Regex-level tests. Table-driven, because the interesting cases are the near-misses."""

from __future__ import annotations

import pytest

from verifier.extraction import patterns

NEUTRAL_MATCHES = [
    ("[2007] SGCA 37", ("2007", "SGCA", "37")),
    ("[2020] SGHC(A) 4", ("2020", "SGHC(A)", "4")),
    ("[2023] SGCA(I) 1", ("2023", "SGCA(I)", "1")),
    ("[2021] SGHC 100", ("2021", "SGHC", "100")),
    ("[2019] SGHCF 12", ("2019", "SGHCF", "12")),
    ("[2018] SGIPOS 7", ("2018", "SGIPOS", "7")),
    ("[2022] SGPDPC 3", ("2022", "SGPDPC", "3")),
    ("see [2007] SGCA 37, which held", ("2007", "SGCA", "37")),
]


@pytest.mark.parametrize(("text", "expected"), NEUTRAL_MATCHES)
def test_neutral_citation_matches(text: str, expected: tuple[str, str, str]) -> None:
    match = patterns.NEUTRAL_CITATION.search(text)
    assert match is not None
    assert (match.group("year"), match.group("court"), match.group("number")) == expected


NEUTRAL_NON_MATCHES = [
    "[2020] ABC 12",  # courts are enumerated, never [A-Z]+
    "[2020] IRAS 4",  # a real acronym, not a court
    "[2007] 4 SLR(R) 100",  # report citation
    "[2020] SGCA",  # no number
    "2007 SGCA 37",  # no brackets
    "[1899] SGCA 1",  # year outside 19xx/20xx
]


@pytest.mark.parametrize("text", NEUTRAL_NON_MATCHES)
def test_neutral_citation_rejects(text: str) -> None:
    """A generic [A-Z]+ court class turns every acronym into a citation we then fail
    to resolve -- i.e. into a fabrication claim against text that cited nothing."""
    assert patterns.NEUTRAL_CITATION.search(text) is None


def test_sghc_a_beats_sghc_in_the_alternation() -> None:
    """Ordering in the court alternation is load-bearing: `|` is first-match, so a
    shorter prefix listed first would leave '(A) 4' unparsed."""
    match = patterns.NEUTRAL_CITATION.search("[2020] SGHC(A) 4")
    assert match is not None and match.group("court") == "SGHC(A)"


REPORT_MATCHES = [
    ("[2007] 4 SLR(R) 100", "SLR(R)", "100"),
    ("[2021] 1 SLR 55", "SLR", "55"),
    ("[1932] AC 562", "AC", "562"),
    ("[1978] 1 WLR 1520", "WLR", "1520"),
    ("[1996] 1 MLJ 123", "MLJ", "123"),
    ("[1963] 2 All ER 575", "All ER", "575"),
    ("[1964] 2 QB 1", "QB", "1"),
    ("[1990] Ch 1", "Ch", "1"),
    ("[2001] EWCA Civ 1249", "EWCA Civ", "1249"),
    ("[2003] EWHC 1319", "EWHC", "1319"),
    ("[1978] UKHL 4", "UKHL", "4"),
    ("[2011] UKSC 50", "UKSC", "50"),
]


@pytest.mark.parametrize(("text", "series", "page"), REPORT_MATCHES)
def test_report_citation_matches(text: str, series: str, page: str) -> None:
    match = patterns.REPORT_CITATION.search(text)
    assert match is not None
    assert match.group("series").replace("  ", " ") == series
    assert match.group("page") == page


@pytest.mark.parametrize("text", ["see Ch 1 of the report", "AC 562", "[2007] SGCA 37"])
def test_report_citation_rejects(text: str) -> None:
    assert patterns.REPORT_CITATION.search(text) is None


PINPOINT_MATCHES = [
    ("at [115]", 115),
    ("at para 115", 115),
    ("at paras 115", 115),
    ("at paragraph 42", 42),
    ("at para [42]", 42),
    ("at [115]-[117]", 115),
]


@pytest.mark.parametrize(("text", "expected"), PINPOINT_MATCHES)
def test_pinpoint_matches(text: str, expected: int) -> None:
    match = patterns.PINPOINT.search(text)
    assert match is not None
    assert int(match.group("bracketed") or match.group("plain")) == expected


@pytest.mark.parametrize("text", ["at 294", "at page 294", "at [2021] 1 SLR 55"])
def test_pinpoint_rejects_page_and_year_references(text: str) -> None:
    """'at 294' is a PAGE into a law report and 'at [2021] 1 SLR 55' is a citation
    year. Reading either as a paragraph points verification at a paragraph that does
    not exist, turning a checkable quote into an unverifiable one."""
    match = patterns.PINPOINT.search(text)
    assert match is None or match.group("bracketed") is None
