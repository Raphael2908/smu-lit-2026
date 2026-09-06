"""L1's sub-check 1a -- does this citation exist, and is it the right document?

This is the hallucination defence proper, and it is deterministic: every answer here
comes from a closed index and a fetch, so the same citation gives the same verdict
twice. Nothing in this file calls a model.

That property used to be untrue. This layer also carried "is the output cited at all?"
as sub-check 1a, and that check counts what an LLM extractor returned -- which meant the
layer badged deterministic had a language model at the top of it. Citedness is now L0's
gate (``layers/l0_preprocessing.py``), the sub-checks behind it moved up, and the badge
is honest. See that module for why the check belongs next to the model call that feeds it.

THE GOVERNING RULE: "cannot verify" is never "fabricated."

Only positive evidence of non-existence may FAIL. A report-only citation that the
full-text index cannot resolve (F7), a login-walled source whose session expired, a
site outage (F12) and a resolver error are all states where we *did not find out*.
Each of those is a WARN. Failing them would mean that during an eLitigation
maintenance window this system reports every real Singapore case as hallucinated --
the worst failure this product can have, and one that looks like a working demo right
up until it isn't. Fail-fast makes a false FAIL unrecoverable, so we prefer a false
green to a false red.

This layer does NOT check whether a quotation appears in the document it cites. That
check was removed: it turned on a fuzzy 75/90 ``partial_ratio`` band, which is a
judgement dressed as a measurement. Whether the output is faithful to what the source
says is L2's question, asked with a tool that suits it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedCitation,
    Resolution,
)
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import (
    CitationType,
    FindingCode,
    Layer,
    LayerStatus,
    ResolutionStatus,
    Severity,
    SubLayer,
)
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.layers.base import BaseLayer, status_from_findings, sub_result
from verifier.settings import Settings, get_settings

# --- normalisation -----------------------------------------------------------------

#: Characters that differ between what a model writes and what a court publishes.
#: Every one of these alone is enough to make ``quote in body`` return False on a
#: verbatim quotation, which is why exact substring matching is not usable here.
_TRANSLATE = {
    "‘": "'",  # ' left single quote
    "’": "'",  # ' right single quote / apostrophe
    "‚": "'",
    "‛": "'",
    "“": '"',  # " left double quote
    "”": '"',  # " right double quote
    "„": '"',
    "‟": '"',
    "′": "'",  # prime
    "″": '"',  # double prime
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "-",  # em dash
    "―": "-",  # horizontal bar
    "−": "-",  # minus sign
}

#: ' v ', ' v. ', ' vs ', ' vs. ' -- how party names are separated in a case name.
_PARTY_SPLIT = re.compile(r"\s+vs?\.?\s+", re.IGNORECASE)

#: Party names shorter than this match almost anything under ``partial_ratio``.
#: 'AG' would score 100 against any body containing the letters a and g in sequence.
_MIN_PARTY_CHARS = 4

#: Resolution preference within a cluster. A cluster is ONE logical reference written
#: in several forms; we judge it by the most authoritative form that was actually
#: attempted, which is what rescues report-only citations travelling with a neutral one.
_TYPE_ORDER = {
    CitationType.NEUTRAL: 0,
    CitationType.CASE_NAME: 1,
    CitationType.URL: 2,
    CitationType.REPORT: 3,
}

#: How each kind of uncited assertion is described to the user. Phrased as what the
#: output *did*, not as an accusation: at this stage we know only that authority is
#: absent, which is a different claim from the assertion being wrong.
#: Why a citation was UNRESOLVABLE. One sentence cannot be honest about all of them.
#:
#: This used to be a single string saying "is a report-only citation, which the full-text
#: index cannot resolve (F7)" -- printed for EVERY unresolvable citation, including an
#: sso.agc.gov.sg URL, which is not a report citation and has nothing to do with the
#: full-text index. Telling a lawyer something specific that we did not establish is a
#: smaller error than a false FAIL, but it is the same KIND of error, and this layer's
#: whole discipline is not making it.
#:
#: Keyed on ``Resolution.detail``, which already carried the truth. Matched on the stem
#: before ":" so "url_not_on_any_source:medium.com" resolves. Deliberately a string
#: lookup and not an import: ``layers/`` does not depend on ``sources/`` today, and one
#: message is not a reason to start.
_UNRESOLVABLE_REASONS: dict[str, str] = {
    "report_citation_not_resolvable": (
        "is a report-only citation, which the full-text index cannot resolve (F7). "
        "It was not checked -- this is not evidence that it is wrong"
    ),
    "url_not_on_this_source": (
        "points at a source outside the corpus this citation was looked up in, so it "
        "was not checked -- this is not evidence that it is wrong"
    ),
    "url_not_on_any_source": (
        "points at a source we have no adapter for, so it was not checked -- this is "
        "not evidence that it is wrong"
    ),
    "no_adapter_for_citation_type": (
        "is a citation form we cannot resolve to any source, so it was not checked -- "
        "this is not evidence that it is wrong"
    ),
    "no_url_for_citation": (
        "could not be turned into a source URL, so it was not checked -- this is not "
        "evidence that it is wrong"
    ),
    "empty_case_name": (
        "carried no case name to search for, so it was not checked -- this is not "
        "evidence that it is wrong"
    ),
    "sso_resolves_urls_only": (
        "is a statutory reference, which we can only check when it is accompanied by a "
        "link. It was not checked -- this is not evidence that it is wrong"
    ),
    "not_a_legislation_url": (
        "links to a page that is not a legislation document, so it was not checked -- "
        "this is not evidence that it is wrong"
    ),
}

#: What to say when the detail is absent or unrecognised. Says only what is known.
_UNRESOLVABLE_DEFAULT = (
    "could not be resolved to any source we cover, so it was not checked -- this is not "
    "evidence that it is wrong"
)


def _unresolvable_explanation(detail: str | None) -> str:
    return _UNRESOLVABLE_REASONS.get((detail or "").split(":", 1)[0], _UNRESOLVABLE_DEFAULT)


#: Resolution states that mean "we did not find out". All WARN, never FAIL.
_UNVERIFIED_STATES: dict[ResolutionStatus, tuple[FindingCode, str]] = {
    ResolutionStatus.UNRESOLVABLE: (
        FindingCode.CITATION_UNVERIFIED,
        # Placeholder: the real sentence comes from _unresolvable_explanation(detail).
        _UNRESOLVABLE_DEFAULT,
    ),
    ResolutionStatus.UNAUTHENTICATED: (
        FindingCode.SOURCE_UNAUTHENTICATED,
        "could not be checked because the source required a login we do not hold. "
        "Being unable to check a citation is not evidence that it was fabricated",
    ),
    ResolutionStatus.ERROR: (
        FindingCode.CITATION_UNVERIFIED,
        "could not be checked because the lookup failed. "
        "Being unable to check a citation is not evidence that it was fabricated",
    ),
    ResolutionStatus.AMBIGUOUS: (
        FindingCode.CITATION_AMBIGUOUS,
        "matched more than one document and none confidently. It was not verified",
    ),
}


@dataclass(frozen=True)
class NormalizedText:
    """Normalised text plus a per-character map back into the source string.

    The map is what lets ``Evidence.best_match_text`` show the reader the ORIGINAL
    passage -- properly cased, with its real punctuation -- rather than the mangled
    lowercase form we matched against. An accuracy tool that shows its working has to
    show working a human can read.
    """

    text: str
    #: ``origin[i]`` is the index in the source string that produced ``text[i]``.
    origin: tuple[int, ...]

    def source_slice(self, source: str, start: int, end: int) -> str:
        if not self.origin or start >= end or start >= len(self.origin):
            return ""
        end = min(end, len(self.origin))
        return source[self.origin[start] : self.origin[end - 1] + 1].strip()


def normalize(text: str) -> NormalizedText:
    """NFKD, curly quotes -> straight, dashes -> hyphen, collapse whitespace, casefold.

    Done per source character so the offset map stays exact even where NFKD or
    casefolding changes the character count ("fi" -> "fi", "ss" -> "ss").
    """
    chars: list[str] = []
    origin: list[int] = []
    pending_space = False
    for index, raw in enumerate(text):
        char = _TRANSLATE.get(raw, raw)
        if char.isspace():
            # Collapse runs; a leading run is dropped because ``chars`` is still empty,
            # a trailing run because ``pending_space`` is never flushed.
            pending_space = bool(chars)
            continue
        if pending_space:
            chars.append(" ")
            origin.append(index)
            pending_space = False
        for expanded in unicodedata.normalize("NFKD", char).casefold():
            chars.append(expanded)
            origin.append(index)
    return NormalizedText("".join(chars), tuple(origin))


def citation_token(text: str) -> str:
    """'[2007] SGCA 37' -> '2007sgca37'; 'SGHC(A)' -> 'sghca' (F10)."""
    return "".join(ch for ch in text if ch.isalnum()).casefold()


def expected_citation_token(citation: ExtractedCitation) -> str | None:
    if citation.court and citation.year and citation.number:
        return citation_token(f"{citation.year}{citation.court}{citation.number}")
    return None


def party_names(case_name: str) -> list[str]:
    """Split a case name into party names, dropping any trailing citation."""
    head = case_name.split("[")[0]
    parts = [p.strip(" ,;.–-") for p in _PARTY_SPLIT.split(head)]
    return [p for p in parts if len(p) >= _MIN_PARTY_CHARS]


# --- document view -----------------------------------------------------------------


@dataclass
class _DocumentView:
    """Per-run cache of a document's normalised forms.

    A judgment body is ~84k characters and normalisation is a full pass over it, so it
    happens at most once per document per run no matter how many citations name it.
    """

    document: SourceDocument
    _body: NormalizedText | None = None

    @property
    def body(self) -> NormalizedText:
        if self._body is None:
            self._body = normalize(self.document.text)
        return self._body


# --- the layer ---------------------------------------------------------------------


class CitationExistenceLayer(BaseLayer):
    """L1's 1a. Reads ``data.resolutions``, ``data.documents`` and ``data.extraction``.

    It fetches nothing itself. Resolution happens once, up front, in a shared
    single-flight resolver that populates both ``resolutions`` and ``documents``, so L1
    and L2 are served by the same fetch and L2 never waits on L1's verdict.

    The layer is PURE with respect to ``LayerInput``: it holds no repository and touches
    no database, which is what makes every case below testable by constructing contract
    objects and nothing else.
    """

    layer = Layer.L1_CITATION_INTEGRITY

    async def _run(self, data: LayerInput) -> LayerResult:
        settings = get_settings()
        findings: list[Finding] = []
        clusters = data.extraction.clusters
        views: dict[str, _DocumentView | None] = {}

        counts = {
            "clusters": len(clusters),
            "resolved": 0,
            "not_found": 0,
            "unverified": 0,
            "no_resolution": 0,
            "no_document": 0,
        }

        for cluster in clusters:
            resolution = _resolution_for(cluster, data.resolutions)
            findings.extend(self._check_cluster(data, cluster, resolution, views, counts, settings))

        if not clusters:
            # Nothing was cited, so there is no citation to look up. Whether that is a
            # problem is L0's question, already asked and answered before this layer ran.
            return LayerResult(layer=self.layer, status=LayerStatus.NOT_APPLICABLE)

        all_findings = tuple(findings)

        # No score. Quote matching was the only numeric signal this layer ever had, and
        # the verdict never read it: `aggregate.deterministic_verdict` works from finding
        # severities alone.
        return LayerResult(
            layer=self.layer,
            status=status_from_findings(all_findings),
            findings=all_findings,
            sub_results=(
                sub_result(
                    SubLayer.L1A_EXISTENCE,
                    all_findings,
                    ran=bool(clusters),
                    detail={
                        "clusters": counts["clusters"],
                        "resolved": counts["resolved"],
                        "not_found": counts["not_found"],
                        "unverified": counts["unverified"],
                    },
                ),
            ),
            detail=dict(counts),
        )

    # -- citations ------------------------------------------------------------

    def _check_cluster(
        self,
        data: LayerInput,
        cluster: CitationCluster,
        resolution: Resolution | None,
        views: dict[str, _DocumentView | None],
        counts: dict[str, int],
        settings: Settings,
    ) -> list[Finding]:
        preferred = cluster.preferred
        label = preferred.raw_text.strip() or f"citation {cluster.ordinal}"

        def make(code: FindingCode, severity: Severity, message: str, evidence: Evidence):
            return Finding(
                id=f"{data.run_id}:L1a:cite:{cluster.ordinal}:{code.value}",
                layer=self.layer,
                sub_layer=SubLayer.L1A_EXISTENCE,
                code=code,
                severity=severity,
                message=message,
                citation_ordinal=cluster.ordinal,
                output_span=cluster.span,
                evidence=evidence,
            )

        if resolution is None:
            # No resolution attempt was recorded. We did not check it, so we say so --
            # silence would present an unchecked citation as a cleared one.
            counts["no_resolution"] += 1
            return [
                make(
                    FindingCode.CITATION_UNVERIFIED,
                    Severity.WARN,
                    f"{label} was not checked: no resolution was attempted for it.",
                    Evidence(extra={"citation_key": preferred.citation_key}),
                )
            ]

        base_extra = {
            "citation_key": resolution.citation_key,
            "resolution_status": resolution.status.value,
            "resolution_method": resolution.method.value,
        }

        if resolution.status is ResolutionStatus.NOT_FOUND:
            # The ONLY citation-level FAIL. Positive evidence of non-existence:
            # eLitigation answers a fabricated neutral citation with HTTP 200 and a
            # ~3.5kB soft-404 whose <title> is empty (F3), and a fabricated case name
            # returns zero search hits (F6). The resolver has already distinguished
            # that from the 819-byte maintenance page, whose <title> is non-empty (F12).
            counts["not_found"] += 1
            return [
                make(
                    FindingCode.CITATION_NOT_FOUND,
                    Severity.FAIL,
                    f"{label} does not exist. The source was reachable and reported no "
                    "such document.",
                    Evidence(
                        source_url=resolution.url,
                        extra={**base_extra, "detail": resolution.detail},
                    ),
                )
            ]

        if resolution.status in _UNVERIFIED_STATES:
            code, explanation = _UNVERIFIED_STATES[resolution.status]
            if resolution.status is ResolutionStatus.UNRESOLVABLE:
                explanation = _unresolvable_explanation(resolution.detail)
            counts["unverified"] += 1
            evidence = Evidence(
                source_url=resolution.url,
                extra={
                    **base_extra,
                    "detail": resolution.detail,
                    **(
                        {"candidates": list(resolution.candidates)} if resolution.candidates else {}
                    ),
                },
            )
            return [make(code, Severity.WARN, f"{label} {explanation}.", evidence)]

        counts["resolved"] += 1
        findings: list[Finding] = []

        wrong_doc = _wrong_document(preferred, resolution)
        if wrong_doc is not None:
            expected, actual = wrong_doc
            return [
                make(
                    FindingCode.RESOLVED_WRONG_DOC,
                    Severity.FAIL,
                    f"{label} resolved to a different document: the page is titled "
                    f"{actual!r}, not {expected!r}.",
                    Evidence(
                        source_url=resolution.url,
                        best_match_text=actual,
                        extra={**base_extra, "expected_title": expected},
                    ),
                )
            ]

        view = self._view(resolution, data, views)
        case_name = _case_name(cluster)
        if view is None:
            # The citation exists and the page is the right one -- the EXISTENCE question
            # is fully answered without the text. Only the text-dependent checks are
            # missing, so we WARN exactly when one of them was actually called for.
            # Warning on every clean citation would train a reader to ignore warnings,
            # which is its own kind of false red.
            if not case_name:
                return findings
            counts["no_document"] += 1
            return [
                make(
                    FindingCode.CITATION_UNVERIFIED,
                    Severity.WARN,
                    f"{label} exists, but its text was not available, so the case name "
                    f"attached to it could not be checked.",
                    Evidence(
                        source_url=resolution.url,
                        extra={**base_extra, "pending": ["case name"]},
                    ),
                )
            ]

        mismatch = self._party_mismatch(case_name, view, settings)
        if mismatch is not None:
            score, parties, checked = mismatch
            findings.append(
                make(
                    FindingCode.CITATION_CASE_NAME_MISMATCH,
                    Severity.FAIL,
                    f"{label} exists, but the case name given does not match it: none of "
                    f"the party names appear in the judgment (best {score:.0f}%).",
                    Evidence(
                        score=score,
                        threshold=settings.L1_PARTY_MATCH_MIN,
                        source_url=resolution.url,
                        extra={**base_extra, "parties": parties, "party_scores": checked},
                    ),
                )
            )
        return findings

    def _party_mismatch(
        self,
        case_name: str | None,
        view: _DocumentView,
        settings: Settings,
    ) -> tuple[float, list[str], dict[str, float]] | None:
        """A REAL citation attached to the WRONG case name.

        We fail only when NO party name is found in the judgment. Requiring every party
        to match would red-flag honest work: parties are routinely abbreviated after
        first mention, and the judgment may write '&' where the output writes 'and'.
        One party matching is enough to believe this is the right case; none matching is
        positive evidence that the citation and the case name do not belong together.
        """
        if not case_name:
            return None
        parties = party_names(case_name)
        if not parties:
            return None

        body = view.body.text
        checked = {
            party: float(fuzz.partial_ratio(normalize(party).text, body)) for party in parties
        }
        best = max(checked.values())
        if best >= settings.L1_PARTY_MATCH_MIN:
            return None
        return best, parties, checked

    # -- documents ------------------------------------------------------------------

    def _view(
        self,
        resolution: Resolution | None,
        data: LayerInput,
        views: dict[str, _DocumentView | None],
    ) -> _DocumentView | None:
        """The document for a resolution, cached per run.

        ``data.documents`` is keyed by ``citation_key``, exactly like ``resolutions``. A
        document with no text is treated as absent: an empty body cannot verify or
        refute anything, and pretending otherwise would score every quote against "".
        """
        if resolution is None or not resolution.is_resolved:
            return None
        key = resolution.citation_key
        if key not in views:
            document = data.documents.get(key)
            views[key] = _DocumentView(document) if document is not None and document.text else None
        return views[key]


def _case_name(cluster: CitationCluster) -> str | None:
    """The case name the output asserted for this cluster, from whichever member carries
    it -- a neutral citation and its case-name sibling are one reference."""
    for member in sorted(cluster.members, key=lambda c: _TYPE_ORDER[c.citation_type]):
        if member.case_name:
            return member.case_name
    return None


def _resolution_for(
    cluster: CitationCluster, resolutions: dict[str, Resolution]
) -> Resolution | None:
    """The most authoritative resolution attempted for this cluster.

    A cluster is one logical reference written in several forms. Preferring the neutral
    citation is what rescues a report-only citation travelling with a resolvable
    sibling (F7) -- and, in the other direction, means a fabricated neutral citation
    still fails even when a real case name sits beside it.
    """
    for member in sorted(cluster.members, key=lambda c: _TYPE_ORDER[c.citation_type]):
        found = resolutions.get(member.citation_key)
        if found is not None:
            return found
    return None


def _wrong_document(citation: ExtractedCitation, resolution: Resolution) -> tuple[str, str] | None:
    """Did the resolved page turn out to be a different case?

    Real judgment pages carry the neutral citation verbatim in <title> (F4), so this is
    a free cross-check. If no title came back at all we say nothing: an absent title is
    not evidence of the wrong document.
    """
    expected = expected_citation_token(citation)
    if not expected:
        return None
    title = (resolution.title or "").strip()
    if not title:
        return None
    if expected in citation_token(title):
        return None
    return citation.raw_text.strip() or expected, title
