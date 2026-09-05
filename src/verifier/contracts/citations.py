"""Citations, quotes and their resolution. Frozen contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from verifier.contracts.enums import (
    AttributionMethod,
    CitationType,
    FetchStrategy,
    ResolutionMethod,
    ResolutionStatus,
)


class Span(BaseModel):
    """Character offsets into the AI output, so the UI can highlight the exact text."""

    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ExtractedCitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    ordinal: int
    raw_text: str
    citation_type: CitationType
    span: Span

    # Neutral citations only; the canonical key is f"{court}:{year}:{number}".
    court: str | None = None
    year: int | None = None
    number: int | None = None
    #: Parties as written, used for the cross-check that catches a real citation
    #: attached to the wrong case name.
    case_name: str | None = None
    url: str | None = None

    @property
    def citation_key(self) -> str:
        if self.court and self.year and self.number:
            return f"{self.court.lower()}:{self.year}:{self.number}"
        return f"raw:{self.raw_text.strip().lower()}"


class CitationCluster(BaseModel):
    """One logical reference, however many forms it was written in.

    'Spandeck ... v DSTA [2007] 4 SLR(R) 100; [2007] SGCA 37' is three extracted
    citations but ONE cluster with one resolution attempt. This is what rescues
    report-only citations (F7): in practice they travel with a resolvable sibling.
    Resolution preference: neutral -> case name -> report.
    """

    model_config = ConfigDict(frozen=True)

    ordinal: int
    members: tuple[ExtractedCitation, ...]
    span: Span

    @property
    def preferred(self) -> ExtractedCitation:
        order = {
            CitationType.NEUTRAL: 0,
            CitationType.CASE_NAME: 1,
            CitationType.URL: 2,
            CitationType.REPORT: 3,
        }
        return min(self.members, key=lambda c: order[c.citation_type])


class ExtractedQuote(BaseModel):
    """Text presented as a DIRECT QUOTATION.

    ``delimiter`` is required and load-bearing, not decorative. Lexical matching is
    anti-correlated on paraphrase -- a genuine paraphrase scores LOWER than a
    fabrication (F8) -- so L1 must only ever score text that was actually presented
    as a quote. Paraphrased attributions are L3's job.
    """

    model_config = ConfigDict(frozen=True)

    ordinal: int
    text: str
    span: Span
    delimiter: str  # '"' | '“' | "'" | 'blockquote' -- provenance that this IS a quote
    attributed_cluster_ordinal: int | None = None
    attribution_method: AttributionMethod = AttributionMethod.NONE
    #: From 'at [115]'. Narrows verification to one paragraph instead of ~84k chars,
    #: which is a large precision win for partial_ratio.
    pinpoint_paragraph: int | None = None


class Resolution(BaseModel):
    """The outcome of trying to turn a citation into a real document."""

    model_config = ConfigDict(frozen=True)

    citation_key: str
    status: ResolutionStatus
    method: ResolutionMethod = ResolutionMethod.NONE
    url: str | None = None
    domain: str | None = None  # feeds L2b -- a bare citation has no domain until now
    fetch_strategy: FetchStrategy | None = None
    document_id: str | None = None
    title: str | None = None
    case_name: str | None = None
    candidates: tuple[str, ...] = ()
    confidence: float = 0.0
    cached: bool = False
    detail: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED
