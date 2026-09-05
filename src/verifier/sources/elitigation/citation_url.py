"""Neutral citation <-> eLitigation URL.

F1: ``[2007] SGCA 37`` -> ``https://www.elitigation.sg/gd/s/2007_SGCA_37``. The mapping
is total and deterministic, which is why L1 needs no model and no threshold -- it is a
lookup, not a judgement.

F10 is the trap: ``SGHC(A)`` becomes ``SGHCA``. The parentheses are STRIPPED, not
percent-encoded. ``%28A%29`` returns a soft-404, and a soft-404 is HTTP 200 (F3), so
getting this wrong does not raise -- it silently reports every Appellate Division case
as fabricated.
"""

from __future__ import annotations

import re

from verifier.contracts.citations import ExtractedCitation
from verifier.contracts.enums import CitationType
from verifier.extraction import patterns
from verifier.settings import settings

#: Path prefix for a judgment on eLitigation's "SG Courts Judgments" viewer.
JUDGMENT_PATH_PREFIX = "/gd/s/"

#: ``/gd/s/2007_SGCA_37`` -> year, court, number. Used to recover the citation a URL was
#: built from, so ``parse()`` can check the page it got back is the page it asked for.
JUDGMENT_PATH = re.compile(
    r"/gd/s/(?P<year>\d{4})_(?P<court>[A-Za-z]+)_(?P<number>\d{1,4})(?:[/?#].*)?$"
)


def normalise_court(court: str) -> str:
    """``SGHC(A)`` -> ``SGHCA`` (F10). Parentheses are removed, not encoded."""
    return court.replace("(", "").replace(")", "").replace(" ", "").upper()


def citation_slug(year: int, court: str, number: int) -> str:
    """``2007``, ``SGCA``, ``37`` -> ``2007_SGCA_37``."""
    return f"{year}_{normalise_court(court)}_{number}"


def build_url(citation: ExtractedCitation | str, *, base_url: str | None = None) -> str | None:
    """Deterministic judgment URL, or None when the citation is not a neutral one.

    Accepts either an ``ExtractedCitation`` or a raw string like "[2007] SGCA 37".
    Returning None (rather than guessing) is what keeps report citations out of the
    fetch path entirely: they are unresolvable by construction (F7), not missing.
    """
    base = (base_url or settings.ELITIGATION_BASE_URL).rstrip("/")

    if isinstance(citation, str):
        match = patterns.NEUTRAL_CITATION.search(citation)
        if not match:
            return None
        year = int(match.group("year"))
        court = match.group("court")
        number = int(match.group("number"))
    else:
        if citation.citation_type is not CitationType.NEUTRAL:
            return None
        if citation.year is None or citation.court is None or citation.number is None:
            return None
        year, court, number = citation.year, citation.court, citation.number

    return f"{base}{JUDGMENT_PATH_PREFIX}{citation_slug(year, court, number)}"


def citation_from_url(url: str) -> str | None:
    """``.../gd/s/2007_SGCA_37`` -> ``[2007] SGCA 37``.

    The parser uses this to learn what citation the page was *supposed* to be, which is
    the whole basis of the three-state classification: the discriminator is whether the
    page's <title> equals the citation we asked for.

    Court codes cannot be un-normalised (``SGHCA`` could only ever have come from
    ``SGHC(A)``), so we reverse the F10 stripping against the known court list.
    """
    match = JUDGMENT_PATH.search(url)
    if not match:
        return None
    slug_court = match.group("court").upper()
    court = next(
        (c for c in patterns.SG_COURT_CODES if normalise_court(c) == slug_court), slug_court
    )
    return f"[{match.group('year')}] {court} {int(match.group('number'))}"


def is_judgment_url(url: str) -> bool:
    return JUDGMENT_PATH.search(url) is not None
