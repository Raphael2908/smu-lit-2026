"""Singapore Statutes Online: open legislation, reachable only through a browser."""

from __future__ import annotations

from verifier.sources.sso.client import SearchUnavailable, SsoAdapter
from verifier.sources.sso.parser import Classification, PageState

__all__ = ["Classification", "PageState", "SearchUnavailable", "SsoAdapter"]
