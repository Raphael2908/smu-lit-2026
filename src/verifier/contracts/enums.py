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

    FAIL from any of L1-L3 skips the judge entirely (fail-fast gate).
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
    """The four scoring layers, plus the extraction pre-pass.

    There used to be five, because source trust was given a layer of its own. It is not
    a peer of "does the argument follow from the source?" -- it is one of the things
    Layer 1 asks about a citation, and it now sits inside Layer 1 as sub-check 1c (see
    ``SubLayer``). Everything after it moved down one.
    """

    #: Not scored, and it cannot move a verdict. Finds the citations, quotes,
    #: propositions and domains every scoring layer works from.
    L0_EXTRACT = "L0"
    #: 1a cited at all -> 1b resolves to a real document -> 1c the domain is trusted.
    L1_CITATION_INTEGRITY = "L1"
    L2_ALIGNMENT = "L2"
    L3_RESPONSIVENESS = "L3"
    L4_JUDGE = "L4"


class SubLayer(StrEnum):
    """The named checks inside Layer 1, in the order they depend on each other.

    Layer 1 asks one question -- "is the citation integrity of this answer sound?" --
    and answers it in three parts. They are a real contract, not prose: each finding
    carries the sub-check that raised it, and ``LayerResult.sub_results`` reports each
    one's status, so a reader can see WHICH part failed without the layer having to
    split into three rows the API would then have to call layers.

    Values match the strings these checks were already tagged with informally, so
    nothing downstream has to relearn the vocabulary.
    """

    #: Does the output offer any authority at all? A count over the whole output.
    L1A_CITEDNESS = "L1a"
    #: Does each citation resolve to a real document, and the right one?
    L1B_EXISTENCE = "L1b"
    #: Is the domain it resolved to one we trust? (black / gray / white lists)
    L1C_SOURCE_TRUST = "L1c"


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
    """How a quote or proposition was tied to a citation. Descending confidence."""

    PINPOINT = "pinpoint"  # 'at [115]' -- also narrows the search scope
    EXPLICIT = "explicit"  # same sentence
    PROXIMITY = "proximity"  # same paragraph, <=400 chars
    #: Governed by a citation earlier in the same paragraph, at any distance. Legal
    #: writing cites once and then discusses for several sentences; without this,
    #: every sentence after the first would read as uncited. L1a only.
    CARRIED = "carried"
    NONE = "none"  # unattributed -> INFO, never a failure


class PropositionKind(StrEnum):
    """Why a sentence was judged to require authority (L1a).

    Narrow by design: only sentences carrying an explicit legal-assertion cue qualify.
    A classifier that fires on ordinary framing prose would manufacture uncited-claim
    findings against correct legal writing, which is the same class of error as a false
    fabrication claim.
    """

    HOLDING = "holding"  # 'the Court of Appeal held that ...'
    LEGAL_TEST = "legal_test"  # 'the test for a duty of care is ...'
    ESTABLISHED = "established"  # 'it is well established that ...'
    STATUTE = "statute"  # 'under the Act, a developer must ...'


class AuthorityKind(StrEnum):
    """What, if anything, supports a proposition."""

    NONE = "none"
    CITATION = "citation"  # a CitationCluster
    STATUTE = "statute"  # a specific statutory reference


class FindingCode(StrEnum):
    """Every way the system can complain. Stable identifiers -- the UI maps these to copy."""

    # --- L1a: is the proposition supported by any authority at all? ---
    #: The output asserts law and cites nothing, anywhere. The ONLY L1a FAIL, and it
    #: needs no attribution to reach: it is a count over the whole output, so there is
    #: no "which citation covers which claim" judgement in it. Downgraded to WARN on a
    #: follow-up turn, where the authority legitimately sits in the previous turn.
    OUTPUT_UNCITED = "OUTPUT_UNCITED"
    #: One assertion with no citation in scope. WARN by default (L1A_UNCITED_SEVERITY):
    #: attribution in prose has no fixed structure, so coverage is deliberately
    #: generous and this finding's errors fall towards silence, not accusation.
    PROPOSITION_UNCITED = "PROPOSITION_UNCITED"

    # --- L1b: does the citation exist, and is it the right document? ---
    CITATION_NOT_FOUND = "CITATION_NOT_FOUND"
    RESOLVED_WRONG_DOC = "RESOLVED_WRONG_DOC"
    CITATION_CASE_NAME_MISMATCH = "CITATION_CASE_NAME_MISMATCH"
    CITATION_AMBIGUOUS = "CITATION_AMBIGUOUS"
    CITATION_UNVERIFIED = "CITATION_UNVERIFIED"  # report-only (F7): WARN, never FAIL
    SOURCE_UNAUTHENTICATED = "SOURCE_UNAUTHENTICATED"  # WARN: cannot check != fabricated

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

    # --- L4: judge ---
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
