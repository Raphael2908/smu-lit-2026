"""Closed vocabularies shared by every module. Frozen contract -- see docs/02-contracts.md."""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    """Ordered lattice: FAIL < WARN < PASS. Aggregation only ever moves down."""

    FAIL = "fail"
    WARN = "warn"
    PASS = "pass"
    PENDING = "pending"


#: Ordering used by ``lattice_min``. PENDING is deliberately absent: it is a
#: lifecycle state, not a verdict, and comparing it would silently mask bugs.
VERDICT_ORDER: dict[Verdict, int] = {
    Verdict.FAIL: 0,
    Verdict.WARN: 1,
    Verdict.PASS: 2,
}


class Severity(StrEnum):
    """How a finding affects the verdict.

    FAIL from any of L1-L4 skips the judge entirely (fail-fast gate).
    WARN passes but annotates. INFO is reporting only.
    """

    FAIL = "fail"
    WARN = "warn"
    INFO = "info"


class LayerStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    ERROR = "error"
    SKIPPED = "skipped"


class Layer(StrEnum):
    L0_EXTRACT = "L0"
    L1_EXISTENCE = "L1"
    L2_SOURCE_TRUST = "L2"
    L3_GROUNDING = "L3"
    L4_RESPONSIVENESS = "L4"
    L5_JUDGE = "L5"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DETERMINISTIC_READY = "deterministic_ready"
    JUDGING = "judging"
    COMPLETE = "complete"
    ERROR = "error"


class VerdictStage(StrEnum):
    DETERMINISTIC = "deterministic"
    FINAL = "final"


class FindingSource(StrEnum):
    """Whether a finding is machine-checkable ground truth or model opinion.

    The extension renders these differently. That visual separation is the
    'who audits the auditor' answer expressed in UI.
    """

    DETERMINISTIC = "deterministic"
    LLM = "llm"


class CitationType(StrEnum):
    NEUTRAL = "neutral"  # [2007] SGCA 37 -- resolvable to a deterministic URL
    REPORT = "report"  # [2007] 4 SLR(R) 100 -- NOT resolvable (see F7)
    CASE_NAME = "case_name"  # Spandeck Engineering v DSTA -- resolvable via search
    URL = "url"  # a bare link written in the output


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"  # hard evidence of fabrication
    AMBIGUOUS = "ambiguous"  # search hits, none confidently the right case
    UNRESOLVABLE = "unresolvable"  # report-only citation; cannot be checked (F7)
    UNAUTHENTICATED = "unauthenticated"  # login-walled source, session expired
    ERROR = "error"


class ResolutionMethod(StrEnum):
    URL = "url"  # deterministic neutral-citation URL
    SEARCH = "search"  # case-name search
    CACHE = "cache"
    NONE = "none"


class FetchStrategy(StrEnum):
    HTTP = "http"  # open sources: eLitigation, AGC SSO
    BROWSER = "browser"  # login-walled sources: LawNet


class ListType(StrEnum):
    WHITE = "white"
    GRAY = "gray"
    BLACK = "black"


class MatchType(StrEnum):
    DOMAIN = "domain"  # matches the domain and its subdomains
    URL_PATTERN = "url_pattern"
    PUBLISHER = "publisher"


class ChunkKind(StrEnum):
    BODY = "body"
    QUOTE = "quote"
    HEADING = "heading"
    CLAIM = "claim"
    WINDOW = "window"


class AttributionMethod(StrEnum):
    """How a quote was tied to a citation. Ordered by descending confidence."""

    PINPOINT = "pinpoint"  # 'at [115]' -- also narrows the search scope
    EXPLICIT = "explicit"  # same sentence
    PROXIMITY = "proximity"  # same paragraph, <=400 chars
    NONE = "none"  # unattributed -> INFO, never a failure


class FindingCode(StrEnum):
    """Every way the system can complain. Stable identifiers -- the UI maps these to copy."""

    # --- L1: citation existence and quote verification ---
    CITATION_NOT_FOUND = "CITATION_NOT_FOUND"
    RESOLVED_WRONG_DOC = "RESOLVED_WRONG_DOC"
    CITATION_CASE_NAME_MISMATCH = "CITATION_CASE_NAME_MISMATCH"
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    QUOTE_INEXACT = "QUOTE_INEXACT"
    CITATION_AMBIGUOUS = "CITATION_AMBIGUOUS"
    CITATION_UNVERIFIED = "CITATION_UNVERIFIED"  # report-only (F7): WARN, never FAIL
    SOURCE_UNAUTHENTICATED = "SOURCE_UNAUTHENTICATED"  # WARN: cannot check != fabricated
    QUOTE_UNATTRIBUTED = "QUOTE_UNATTRIBUTED"

    # --- L2: source trust ---
    SOURCE_BLACKLISTED = "SOURCE_BLACKLISTED"
    SOURCE_GRAYLISTED = "SOURCE_GRAYLISTED"
    SOURCE_UNKNOWN = "SOURCE_UNKNOWN"

    # --- L3: source grounding ---
    CLAIM_NOT_GROUNDED_IN_SOURCE = "CLAIM_NOT_GROUNDED_IN_SOURCE"
    CLAIM_WEAKLY_GROUNDED = "CLAIM_WEAKLY_GROUNDED"

    # --- L4: responsiveness ---
    QUESTION_NOT_ANSWERED = "QUESTION_NOT_ANSWERED"
    QUESTION_PARTIALLY_ANSWERED = "QUESTION_PARTIALLY_ANSWERED"
    ANSWER_TOO_SHORT = "ANSWER_TOO_SHORT"
    FOLLOWUP_NOT_SCORED = "FOLLOWUP_NOT_SCORED"

    # --- L5: judge ---
    JUDGE_FAILED_FAITHFULNESS = "JUDGE_FAILED_FAITHFULNESS"
    JUDGE_FAILED_CONTEXTUAL_ACCURACY = "JUDGE_FAILED_CONTEXTUAL_ACCURACY"
    JUDGE_FAILED_CITATION_INTEGRITY = "JUDGE_FAILED_CITATION_INTEGRITY"
    JUDGE_FAILED_RESPONSIVENESS = "JUDGE_FAILED_RESPONSIVENESS"
    #: The answer is substantively true but omits something a competent lawyer
    #: needs -- a controlling authority, an essential element, a temporal rule.
    #: Scored independently of correctness: an answer can be right and still
    #: materially misleading by omission.
    JUDGE_FAILED_COMPLETENESS = "JUDGE_FAILED_COMPLETENESS"
    JUDGE_UNPARSEABLE = "JUDGE_UNPARSEABLE"
    JUDGE_ERROR = "JUDGE_ERROR"

    # --- pipeline ---
    LAYER_ERROR = "LAYER_ERROR"
