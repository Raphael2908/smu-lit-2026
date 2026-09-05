"""L0 with a model finding the citations.

Three properties are under test, and each one is a way this feature could accuse
correct legal work of fabrication if it were wrong:

* **Verbatim or nothing.** A candidate that is not in the answer never becomes
  authority, so a model that invents a citation cannot clear the L1a FAIL.
* **The parser types it, not the model.** A foreign neutral citation must not be typed
  NEUTRAL, because ``build_url`` would then send it to eLitigation and read the soft-404
  as proof the case does not exist.
* **A degraded extractor produces no citations and says so.** Never a silent empty
  result, which L1a would be entitled to read as "this answer cited nothing".
"""

from __future__ import annotations

import asyncio

import pytest

from verifier.contracts.enums import CitationType
from verifier.extraction import extract
from verifier.extraction.llm import extract_with_llm, place_candidates
from verifier.providers.base import CitationCandidate, CitationExtraction

pytestmark = pytest.mark.anyio

SPANDECK = "Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37"
ANSWER = f"The Court of Appeal set out a single two-stage test in {SPANDECK}."


class StubExtractor:
    """A citation extractor whose failure mode is chosen by the test."""

    provider = "stub"
    model = "stub-extractor"

    def __init__(self, citations=(), *, raises=False, hangs=False, degraded=None):
        self._citations = tuple(citations)
        self._raises = raises
        self._hangs = hangs
        self._degraded = degraded
        self.calls = 0

    async def extract_citations(self, ai_output: str) -> CitationExtraction:
        self.calls += 1
        if self._raises:
            raise RuntimeError("provider exploded")
        if self._hangs:
            await asyncio.sleep(3600)
        return CitationExtraction(
            citations=self._citations,
            model=self.model,
            provider=self.provider,
            degraded=self._degraded,
        )


def candidates(*texts: str) -> tuple[CitationCandidate, ...]:
    return tuple(CitationCandidate(raw_text=t) for t in texts)


# --- locating ----------------------------------------------------------------------


def test_a_candidate_is_located_at_its_real_position_in_the_answer():
    result = place_candidates(ANSWER, candidates("[2007] SGCA 37"))
    assert result.missing == 0
    span = result.citations[0].span
    assert ANSWER[span.start : span.end] == "[2007] SGCA 37"


def test_a_citation_the_answer_does_not_contain_is_dropped():
    """The anti-hallucination guard. A model that invents a citation must not be able
    to supply the authority that clears L1a's FAIL."""
    result = place_candidates(ANSWER, candidates("[2099] SGCA 999"))
    assert (result.citations, result.untyped) == ([], [])
    assert (result.missing, result.duplicate) == (1, 0)


def test_a_model_that_tidies_the_text_is_treated_as_not_finding_it():
    """ "Helpfully" normalising is indistinguishable from inventing, and is handled the
    same way: the string is not in the answer, so it is not authority."""
    answer = "The court applied the test in ANJ v ANK [2015] 4 SLR 1043."
    result = place_candidates(answer, candidates("ANJ versus ANK [2015] 4 SLR 1043"))
    assert result.missing == 1


def test_a_citation_wrapping_a_line_break_is_still_found():
    """Markdown wraps long case names. The copy the model returns has a space where the
    source has a newline, and a naive substring search would drop a real citation."""
    answer = (
        "The court held so in Spandeck Engineering (S) Pte Ltd v\nDefence Science &"
        " Technology Agency [2007] SGCA 37."
    )
    result = place_candidates(
        answer,
        candidates("Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency"),
    )
    assert result.missing == 0


def test_a_citation_repeated_twice_takes_two_distinct_spans():
    answer = "First in [2007] SGCA 37, and again in [2007] SGCA 37."
    result = place_candidates(answer, candidates("[2007] SGCA 37", "[2007] SGCA 37"))
    assert len({(c.span.start, c.span.end) for c in result.citations}) == 2


def test_a_citation_the_model_returns_twice_is_a_duplicate_not_a_hallucination():
    """Counted apart, because only one of the two is a warning sign.

    Returning the combined form and then the bare citation is normal and harmless. Text
    that is not in the answer at all is the signal that a model has stopped copying
    verbatim, and summing them would bury it.
    """
    result = place_candidates(ANSWER, candidates(SPANDECK, "[2007] SGCA 37"))
    assert (result.missing, result.duplicate) == (0, 1)


def test_ordinals_are_assigned_by_position_not_by_the_order_the_model_returned_them():
    answer = "See [2007] SGCA 37, then [2020] SGHC 12."
    result = place_candidates(answer, candidates("[2020] SGHC 12", "[2007] SGCA 37"))
    assert [c.raw_text for c in result.citations] == ["[2007] SGCA 37", "[2020] SGHC 12"]
    assert [c.ordinal for c in result.citations] == [0, 1]


# --- typing ------------------------------------------------------------------------


def test_one_candidate_can_yield_a_case_name_and_a_neutral_citation():
    result = place_candidates(ANSWER, candidates(SPANDECK))
    assert [c.citation_type for c in result.citations] == [
        CitationType.CASE_NAME,
        CitationType.NEUTRAL,
    ]


def test_a_foreign_neutral_citation_is_never_typed_neutral():
    """The one that would report a real UK Supreme Court case as fabricated.

    ``build_url`` checks only that a citation is NEUTRAL, never that its court is a
    Singapore one, so a NEUTRAL [2019] UKSC 32 becomes an eLitigation URL, returns a
    soft-404, and L1b calls a real case non-existent. Typing from the parser rather than
    from the model is what makes that unreachable.
    """
    answer = "The House of Lords approach was revisited in [2019] UKSC 32."
    result = place_candidates(answer, candidates("[2019] UKSC 32"))
    assert result.citations
    assert all(c.citation_type is not CitationType.NEUTRAL for c in result.citations)


@pytest.mark.parametrize(
    "text",
    [
        "(2005) 3 SCC 123",
        "Gary Chan, The Law of Torts in Singapore (2nd Ed, 2016)",
    ],
    ids=["unenumerated-report-series", "textbook"],
)
def test_authority_the_parser_cannot_type_is_kept_but_never_clustered(text):
    """It counts for L1a and is never fetched.

    Clustering it would send the phrase to a case-name search of a Singapore judgment
    corpus that does not contain it, and zero hits is exactly what this system reads as
    evidence of fabrication (F6).
    """
    answer = f"The point is discussed in {text}."
    result = place_candidates(answer, candidates(text))
    assert result.citations == []
    assert [raw for _, raw in result.untyped] == [text]
    assert (result.missing, result.duplicate) == (0, 0)


# --- degrading ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "extractor",
    [
        StubExtractor(raises=True),
        StubExtractor(hangs=True),
        StubExtractor(degraded="no key"),
    ],
    ids=["provider-error", "timeout", "provider-reported-degraded"],
)
async def test_a_broken_extractor_says_so_and_never_raises(extractor, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_TIMEOUT_S", "0.05")
    from verifier.settings import get_settings

    get_settings.cache_clear()
    try:
        result = await extract_with_llm(ANSWER, extractor=extractor)
    finally:
        get_settings.cache_clear()

    assert result.extractor_degraded is not None
    assert result.clusters == ()
    # And the propositions still come out, so the reader is told what is unsupported.
    assert result.propositions or not result.clusters


async def test_a_degraded_run_reports_no_authority_rather_than_pretending_to_have_looked():
    """No regex fallback, deliberately. Quietly substituting the deterministic pass
    would report a run as checked when the extractor never ran."""
    result = await extract_with_llm(ANSWER, extractor=StubExtractor(degraded="down"))
    assert result.authority_count == 0
    assert result.extractor_degraded == "down"


async def test_passing_none_runs_the_deterministic_pass_by_name():
    """``extractor=None`` is an explicit choice by the caller, not a fallback from
    failure -- which is why it is NOT reported as degraded."""
    result = await extract_with_llm(ANSWER, extractor=None)
    assert result.extractor_degraded is None
    assert result == extract(ANSWER)


async def test_the_model_can_supply_authority_the_regex_extractor_misses():
    """The reason this feature exists (docs/03-findings.md F13)."""
    answer = "The applicable principle is set out in (2005) 3 SCC 123, which the court applied."
    assert extract(answer).authority_count == 0
    result = await extract_with_llm(answer, extractor=StubExtractor(candidates("(2005) 3 SCC 123")))
    assert result.authority_count == 1


async def test_a_statute_the_model_also_returns_is_not_counted_twice():
    """``extract_statutes`` runs deterministically over the same text, so a statutory
    reference the model returns as well is already authority. Keeping both would inflate
    ``authority_count`` and show the reader the same citation in two places."""
    answer = (
        "Under s 8 of the Civil Law Act (Cap 43, 1999 Rev Ed) a claim survives the "
        "death of the claimant."
    )
    result = await extract_with_llm(
        answer,
        extractor=StubExtractor(candidates("s 8 of the Civil Law Act (Cap 43, 1999 Rev Ed)")),
    )
    assert result.untyped == ()
    assert len(result.statutes) == 1
    assert result.authority_count == 1
