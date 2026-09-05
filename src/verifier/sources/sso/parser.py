"""Singapore Statutes Online: page classification.

THE DISCRIMINATOR IS THE ``<title>``, and length is only a corroborator -- the same
finding F12 recorded for eLitigation, arrived at independently for a different site.
Measured through the adapter's own fetcher against three captured states:

    Act/IA1959      200   345,880 B   "Immigration Act 1959 - Singapore Statutes Online"
    Act/ZZZ9999     200    24,693 B   "Page Not Found - Singapore Statutes Online"
    (WAF refusal)   403       919 B   "ERROR: The request could not be satisfied"

Three states, positively separated, each with a fixture in ``tests/corpus``. That is what
licenses a NOT_FOUND here at all: SSO answers a fabricated Act with HTTP 200, so the
status code carries no signal, exactly as it does not for eLitigation (F3).

The WAF state is why the third fixture had to be captured deliberately rather than
assumed. Its title does not mention Singapore Statutes Online at all, so it cannot be
mistaken for either of the others -- but a rule written to separate only "real" from
"not found" would have classified it as a fabrication, and every SSO citation would have
been reported as hallucinated for as long as the block lasted. F12 is the standing record
of that failure; this is the same trap on a different site.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Classification", "PageState", "classify", "document_title"]

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

#: The site's own name, present in the title of any page SSO itself served.
SITE_SUFFIX = "singapore statutes online"

#: What SSO puts in the title of a document that does not exist. HTTP 200 regardless.
NOT_FOUND_MARKER = "page not found"


class PageState(StrEnum):
    """What came back.

    NOT_FOUND is present now, and was deliberately absent in the first version of this
    file until the three states above had actually been measured. It stays the only
    citation-level FAIL in the system, so the bar for adding one is a positive marker on
    a page SSO served, not an inference from byte count.
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Classification:
    state: PageState
    title: str | None = None
    detail: str | None = None


def document_title(html: str) -> str | None:
    match = _TITLE_RE.search(html or "")
    if match is None:
        return None
    return html_mod.unescape(match.group(1)).strip() or None


def classify(html: str, url: str) -> Classification:
    """Which of the three states this page is in.

    Ordered so that "SSO did not serve this page" is decided FIRST. A CloudFront refusal
    or an empty body must never fall through into a document judgement -- that direction
    is how "we could not check" turns into "this is fabricated".
    """
    if not html or not html.strip():
        return Classification(state=PageState.UNAVAILABLE, detail="empty_body")

    title = document_title(html)
    if title is None:
        return Classification(state=PageState.UNAVAILABLE, detail="no_title")

    lowered = title.casefold()
    if SITE_SUFFIX not in lowered:
        # Something answered that is not SSO: a CDN error page, a captive portal, a WAF
        # refusal. We did not reach the source, so we know nothing about the document.
        return Classification(state=PageState.UNAVAILABLE, title=title, detail="not_served_by_sso")

    if NOT_FOUND_MARKER in lowered:
        return Classification(
            state=PageState.NOT_FOUND, title=title, detail="titled_page_not_found"
        )

    return Classification(state=PageState.FOUND, title=title)
