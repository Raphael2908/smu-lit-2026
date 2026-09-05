"""Findings -- the atoms of a verdict. Frozen contract."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from verifier.contracts.citations import Span
from verifier.contracts.enums import FindingCode, FindingSource, Layer, Severity


class Evidence(BaseModel):
    """Why the system reached its conclusion. Everything a user needs to check our work.

    An accuracy tool that asserts without showing its working has the same
    credibility problem as the thing it audits.
    """

    model_config = ConfigDict(frozen=True)

    score: float | None = None
    threshold: float | None = None
    margin: float | None = None
    #: The passage that best matched -- shown in the panel so a user can judge for themselves.
    best_match_text: str | None = None
    best_match_paragraph: int | None = None
    source_url: str | None = None
    http_status: int | None = None
    body_length: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Finding(BaseModel):
    """One reason the output is (or might be) wrong.

    ``source`` separates machine-checkable ground truth from model opinion. The UI
    renders them differently, and that separation is the visible form of the
    invariant that the judge cannot clear a deterministic failure.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    layer: Layer
    code: FindingCode
    severity: Severity
    message: str
    source: FindingSource = FindingSource.DETERMINISTIC
    citation_ordinal: int | None = None
    quote_ordinal: int | None = None
    output_span: Span | None = None
    evidence: Evidence = Field(default_factory=Evidence)

    @property
    def is_fail(self) -> bool:
        return self.severity is Severity.FAIL
