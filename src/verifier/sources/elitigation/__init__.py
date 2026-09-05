"""eLitigation: Singapore's open judgment corpus. Static HTML, no login, no JS (F2)."""

from __future__ import annotations

from verifier.sources.elitigation.client import ElitigationAdapter
from verifier.sources.elitigation.parser import Classification, PageState

__all__ = ["Classification", "ElitigationAdapter", "PageState"]
