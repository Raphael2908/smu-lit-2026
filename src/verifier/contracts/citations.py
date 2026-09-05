"""Citations, quotes and their resolution. Frozen contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from verifier.contracts.enums import (
    AttributionMethod,
    AuthorityKind,
    CitationType,
    FetchStrategy,
    PropositionKind,
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


class StatuteReference(BaseModel):
    """A statutory reference written in the output.

    Kept OUT of ``CitationCluster`` deliberately. A cluster is something L1b tries to
    resolve against the judgment corpus, and a statute is not in that corpus -- making
    statutes clusters would emit a CITATION_UNVERIFIED warning for every correctly
    cited section. But a statute IS authority for the purposes of L1a, which asks only
    whether a proposition rests on anything at all.

    ``specific`` separates 'section 20 of the Building Control Act (Cap 29)' from a
    bare 'the Act'. Only a specific reference counts as authority on its own; a vague
    one is itself a proposition needing support from something named earlier.
    """

    model_config = ConfigDict(frozen=True)

    ordinal: int
    raw_text: str
    span: Span
    act: str | None = None
    section: str | None = None
    chapter: str | None = None
    specific: bool = True


class ExtractedProposition(BaseModel):
    """A sentence that asserts law, and therefore needs authority behind it (L1a).

    Extraction is NARROW on purpose: a sentence qualifies only on an explicit legal
    assertion cue (a holding verb with a judicial subject, a statement of a test, an
    appeal to settled law, a statutory obligation). Framing, hedges, questions,
    applications to the user's own facts and quoted material are all excluded, because
    a proposition classifier that fires on ordinary prose manufactures uncited-claim
    findings against correct legal writing.

    Coverage is the mirror image: DELIBERATELY GENEROUS. Citation placement in prose
    has no fixed structure -- authority may precede the proposition, follow it, or sit
    once at the head of a paragraph that discusses it for five sentences -- so anything
    in the same paragraph counts. Errors therefore fall towards calling an uncited
    claim cited, never the reverse.
    """

    model_config = ConfigDict(frozen=True)

    ordinal: int
    text: str
    span: Span
    kind: PropositionKind
    #: The cue that classified it, shown in the panel so the user can see our reasoning.
    cue: str
    authority: AuthorityKind = AuthorityKind.NONE
    attribution_method: AttributionMethod = AttributionMethod.NONE
    attributed_cluster_ordinal: int | None = None
    attributed_statute_ordinal: int | None = None

    @property
    def is_cited(self) -> bool:
        return self.authority is not AuthorityKind.NONE


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
