"""Recognising an SSO legislation URL.

There is no inverse. SSO slugs are not derivable from an Act's title -- the Penal Code
1871 is ``PC1871``, the Civil Law Act 1909 is ``CLA1909`` -- and guessing is not a
harmless failure: ``/Act/PenalCode1871`` returns HTTP 200 with a soft-404 body, so a wrong
guess looks exactly like a successful fetch.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

__all__ = ["LEGISLATION_PATH", "is_legislation_url"]

#: The document-bearing paths. Everything else on the host is browse/search chrome.
LEGISLATION_PATH = re.compile(
    r"^/(Act|Act-Rev|Acts-Supp|SL|SL-Supp|Bills-Supp)/[^/]+",
    re.IGNORECASE,
)


def is_legislation_url(url: str | None) -> bool:
    if not url:
        return False
    text = url.strip()
    parts = urlsplit(text if "//" in text else "//" + text)
    return bool(LEGISLATION_PATH.match(parts.path or ""))
