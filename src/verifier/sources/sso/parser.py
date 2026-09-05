"""Singapore Statutes Online: page classification.

PHASE 1. This module exists to hold a shape, not yet to read markup. The real parser is
written against captured pages once ``scripts/sso_probe.py`` has run; see the module
docstring in ``client.py`` for why that ordering is not laziness.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Classification", "PageState", "classify"]


class PageState(StrEnum):
    """What came back.

    NOTE WHAT IS ABSENT: **there is no NOT_FOUND.**

    eLitigation EARNED its NOT_FOUND from a measurement (F3): 150,389 bytes carrying the
    citation in ``<title>``, against 3,549 bytes with an empty one, on a source that
    answers a fabricated citation with HTTP 200. That measurement is what licenses the
    only citation-level FAIL in the system.

    SSO has not been measured. Everything currently known about its soft-404 -- 24,693
    bytes for a bogus slug against 339kB-913kB for a real Act -- was observed over plain
    HTTP, which is *not the path this adapter uses*: SSO answers httpx with 202 and
    ``x-amzn-waf-action: challenge``, so those figures came from a client the adapter is
    not. A NOT_FOUND here would be a fabrication claim manufactured out of an assumption,
    which is the one error this codebase treats as unrecoverable.

    Add NOT_FOUND only when the probe has separated THREE states -- a real Act, a bogus
    slug, and a WAF challenge or outage. Not two. F12 is the standing record of what
    separating only two costs: during a maintenance window every real case gets reported
    as hallucinated.
    """

    FOUND = "found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Classification:
    state: PageState
    title: str | None = None
    detail: str | None = None


def classify(html: str, url: str) -> Classification:
    """Phase 1: anything that came back with a body is UNVERIFIED-but-present.

    Deliberately incapable of concluding absence. The discriminator goes here once it has
    been measured, and ``PageState`` grows its third member in the same commit.
    """
    if not html or not html.strip():
        return Classification(state=PageState.UNAVAILABLE, detail="empty_body")
    return Classification(state=PageState.FOUND)
