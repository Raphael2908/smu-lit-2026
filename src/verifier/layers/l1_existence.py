"""L1 -- citation integrity, in three stages.

This is the hallucination defence, and it runs in the order the questions actually
depend on each other:

* **L1a -- is the proposition cited at all?** Asked first because everything below it
  presumes the output offered authority in the first place. L1b and L1c only ever
  examine citations that were written down, so an answer that states the law from
  memory -- confidently, fluently, with nothing behind it -- would otherwise pass the
  citation-integrity layer without a mark against it. Fabricating no citations is not
  the same as citing correctly.
* **L1b -- does the citation exist, and is it the document the output says it is?**
* **L1c -- is the quoted text actually in that document?**

THE GOVERNING RULE: "cannot verify" is never "fabricated."

Only positive evidence of non-existence may FAIL. A report-only citation that the
full-text index cannot resolve (F7), a login-walled source whose session expired, a
site outage (F12) and a resolver error are all states where we *did not find out*.
Each of those is a WARN. Failing them would mean that during an eLitigation
maintenance window this system reports every real Singapore case as hallucinated --
the worst failure this product can have, and one that looks like a working demo right
up until it isn't. Fail-fast makes a false FAIL unrecoverable, so we prefer a false
green to a false red.

Quote verification only ever scores text that was presented as a DIRECT QUOTATION.
``ExtractedQuote`` carries a required ``delimiter`` for that reason: lexical matching
is anti-correlated on paraphrase -- a genuine paraphrase scores LOWER than a
fabrication (F8) -- so scoring a paraphrase would systematically accuse honest work.
Paraphrased attribution is L3's job, where the tool suits the question.

Fuzzy, not Ctrl+F: curly quotes, en/em dashes, non-breaking spaces, NFKD variants and
case each break exact substring matching on their own, and all of them appear in real
judgment HTML. Normalisation removes what it can; ``partial_ratio`` absorbs the rest.

L1a obeys the same governing rule from the other direction. Deciding *which* citation
supports *which* sentence is a genuine judgement -- authority may precede its
proposition, follow it, or sit once at the head of a paragraph -- so per-proposition
findings are WARN and their coverage rule is deliberately generous (see
``extraction/propositions.py``). The single L1a FAIL asks something with no judgement
in it at all: does this output contain any authority, anywhere? That is a count, which
is why it can carry a verdict that skips the judge.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from verifier.contracts.citations import (
    CitationCluster,
    ExtractedCitation,
    ExtractedProposition,
    ExtractedQuote,
    Resolution,
)
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import (
    AuthorityKind,
    CitationType,
    FindingCode,
    Layer,
    LayerStatus,
    PropositionKind,
    ResolutionStatus,
    Severity,
)
from verifier.contracts.findings import Evidence, Finding
from verifier.contracts.layers import LayerInput, LayerResult
from verifier.layers.base import BaseLayer, status_from_findings
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
_PROPOSITION_MESSAGE: dict[PropositionKind, str] = {
    PropositionKind.HOLDING: "This states what a court decided",
    PropositionKind.LEGAL_TEST: "This states what the law requires",
    PropositionKind.ESTABLISHED: "This appeals to settled law",
    PropositionKind.STATUTE: "This states a statutory rule",
}

#: Resolution states that mean "we did not find out". All WARN, never FAIL.
_UNVERIFIED_STATES: dict[ResolutionStatus, tuple[FindingCode, str]] = {
    ResolutionStatus.UNRESOLVABLE: (
        FindingCode.CITATION_UNVERIFIED,
        "is a report-only citation, which the full-text index cannot resolve (F7). "
        "It was not checked -- this is not evidence that it is wrong",
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
    happens at most once per document per run no matter how many quotes cite it.
    """

    document: SourceDocument
    _body: NormalizedText | None = None
    _paragraphs: dict[int, NormalizedText] = field(default_factory=dict)

    @property
    def body(self) -> NormalizedText:
        if self._body is None:
            self._body = normalize(self.document.text)
        return self._body

    def paragraph(self, number: int) -> tuple[str, NormalizedText] | None:
        paragraph = self.document.paragraph(number)
        if paragraph is None:
            return None
        if number not in self._paragraphs:
            self._paragraphs[number] = normalize(paragraph.text)
        return paragraph.text, self._paragraphs[number]

    def best_paragraph(self, needle: str) -> int | None:
        """Which numbered paragraph the quote most resembles -- evidence only.

        The score reported to the user is always the one computed over the scope the
        rules chose (pinpoint paragraph, else whole body); this only decides which
        paragraph number to show alongside it.
        """
        best: tuple[float, int] | None = None
        for paragraph in self.document.paragraphs:
            if paragraph.paragraph_number is None:
                continue
            found = self.paragraph(paragraph.paragraph_number)
            if found is None:
                continue
            score = fuzz.partial_ratio(needle, found[1].text)
            if best is None or score > best[0]:
                best = (score, paragraph.paragraph_number)
        return best[1] if best else None


# --- the layer ---------------------------------------------------------------------


class CitationExistenceLayer(BaseLayer):
    """L1. Reads ``data.resolutions``, ``data.documents`` and ``data.extraction``.

    It fetches nothing itself. Resolution happens once, up front, in a shared
    single-flight resolver that populates both ``resolutions`` and ``documents``, so L1
    and L3 are served by the same fetch and L3 never waits on L1's verdict.

    The layer is PURE with respect to ``LayerInput``: it holds no repository and touches
    no database, which is what makes every case below testable by constructing contract
    objects and nothing else.
    """

    layer = Layer.L1_EXISTENCE

    async def _run(self, data: LayerInput) -> LayerResult:
        settings = get_settings()
        findings: list[Finding] = []
        clusters = data.extraction.clusters
        quotes = data.extraction.quotes
        by_ordinal = {cluster.ordinal: cluster for cluster in clusters}
        views: dict[str, _DocumentView | None] = {}
        #: Clusters some quotation is hung on, so a missing document can be reported as
        #: an unperformed check rather than passing in silence.
        quoted = {
            quote.attributed_cluster_ordinal
            for quote in quotes
            if quote.attributed_cluster_ordinal is not None
        }

        counts = {
            "clusters": len(clusters),
            "propositions": len(data.extraction.propositions),
            "propositions_cited": 0,
            "propositions_uncited": 0,
            "authorities": data.extraction.authority_count,
            "resolved": 0,
            "not_found": 0,
            "unverified": 0,
            "no_resolution": 0,
            "no_document": 0,
            "quotes_scored": 0,
            "quotes_too_short": 0,
            "quotes_unverifiable": 0,
            "quotes_unattributed": 0,
        }

        # --- L1a, first: is there any authority behind what this output asserts? ----
        findings.extend(self._check_propositions(data, counts, settings))

        for cluster in clusters:
            resolution = _resolution_for(cluster, data.resolutions)
            findings.extend(
                self._check_cluster(
                    data, cluster, resolution, views, counts, settings, cluster.ordinal in quoted
                )
            )

        scores: list[float] = []
        for quote in quotes:
            findings.extend(
                self._check_quote(data, quote, by_ordinal, views, counts, scores, settings)
            )

        checked_propositions = settings.L1A_ENABLED and bool(data.extraction.propositions)
        if not clusters and not quotes and not checked_propositions:
            # Nothing was asserted and nothing was cited. That is a coherent output --
            # a procedural answer, a clarifying question, a refusal -- and there is no
            # citation integrity question to ask about it.
            return LayerResult(layer=self.layer, status=LayerStatus.NOT_APPLICABLE)

        detail: dict[str, object] = dict(counts)
        detail["stages"] = {
            "L1a": "propositions cited at all",
            "L1b": "citations exist and are the right document",
            "L1c": "quotations appear in the cited document",
        }
        if scores:
            # 0-1 for consistency with the cosine-scored layers; the raw 0-100
            # partial_ratio for each quote is on the finding's Evidence.
            detail["score_basis"] = "min_quote_partial_ratio/100"

        return LayerResult(
            layer=self.layer,
            status=status_from_findings(tuple(findings)),
            findings=tuple(findings),
            score=min(scores) / 100.0 if scores else None,
            detail=detail,
        )

    # -- L1a: is the proposition cited at all? --------------------------------------

    def _check_propositions(
        self,
        data: LayerInput,
        counts: dict[str, int],
        settings: Settings,
    ) -> list[Finding]:
        """Whether the output's legal assertions rest on any authority.

        Two findings, doing deliberately different jobs.

        ``OUTPUT_UNCITED`` is the FAIL, and it is reached by counting, not judging: the
        output asserts law and contains no citation and no specific statutory reference
        anywhere. No attribution question arises, so none of the "which citation covers
        which sentence" uncertainty that makes L1a's other finding a WARN applies here.

        ``PROPOSITION_UNCITED`` is per-assertion and WARN by default. Coverage is
        generous by construction, so a proposition reported here had no authority
        anywhere in its scope -- but "scope" is still a heuristic over prose that has no
        fixed citation structure, and a heuristic must not be able to fail a run.
        """
        if not settings.L1A_ENABLED:
            return []
        propositions = data.extraction.propositions
        if not propositions:
            return []

        uncited = [p for p in propositions if not p.is_cited]
        counts["propositions_cited"] = len(propositions) - len(uncited)
        counts["propositions_uncited"] = len(uncited)
        if not uncited:
            return []

        authorities = data.extraction.authority_count
        if authorities == 0 and len(uncited) >= settings.L1A_MIN_ASSERTIONS_FOR_FAIL:
            return [self._output_uncited(data, uncited, propositions)]

        severity = Severity.WARN if settings.L1A_UNCITED_SEVERITY == "warn" else Severity.INFO
        return [self._proposition_uncited(data, p, severity, authorities) for p in uncited]

    def _output_uncited(
        self,
        data: LayerInput,
        uncited: list[ExtractedProposition],
        propositions: tuple[ExtractedProposition, ...],
    ) -> Finding:
        """The whole-output finding: law asserted, nothing cited, anywhere.

        On a FOLLOW-UP turn this is a WARN instead. "What about the second limb?"
        answers a question whose authority was established in the previous turn, and
        demanding that the answer re-cite it would make the single most common shape of
        real conversation fail. This is the same reasoning that makes L4 downgrade a
        follow-up, and for the same reason: under fail-fast a false red is unrecoverable.
        """
        first = uncited[0]
        severity = Severity.WARN if data.is_followup else Severity.FAIL
        followup_note = (
            " This is a follow-up turn, so the authority may have been given earlier in "
            "the conversation; it is reported rather than failed."
            if data.is_followup
            else ""
        )
        return Finding(
            id=f"{data.run_id}:L1a:output",
            layer=self.layer,
            code=FindingCode.OUTPUT_UNCITED,
            severity=severity,
            message=(
                f"This answer states the law but cites no authority at all: "
                f"{len(uncited)} assertion{'s' if len(uncited) != 1 else ''} with no case, "
                f"statute or source behind {'them' if len(uncited) != 1 else 'it'}." + followup_note
            ),
            output_span=first.span,
            evidence=Evidence(
                best_match_text=first.text,
                extra={
                    "stage": "L1a",
                    "assertions": len(propositions),
                    "uncited": len(uncited),
                    "authorities": 0,
                    "is_followup": data.is_followup,
                    "kinds": sorted({p.kind.value for p in uncited}),
                    # Every uncited assertion, so the panel can highlight all of them
                    # under one finding rather than repeating the same message N times.
                    "propositions": [
                        {
                            "ordinal": p.ordinal,
                            "kind": p.kind.value,
                            "cue": p.cue,
                            "text": p.text,
                            "span": {"start": p.span.start, "end": p.span.end},
                        }
                        for p in uncited
                    ],
                },
            ),
        )

    def _proposition_uncited(
        self,
        data: LayerInput,
        proposition: ExtractedProposition,
        severity: Severity,
        authorities: int,
    ) -> Finding:
        return Finding(
            id=f"{data.run_id}:L1a:prop:{proposition.ordinal}",
            layer=self.layer,
            code=FindingCode.PROPOSITION_UNCITED,
            severity=severity,
            message=(
                f"{_PROPOSITION_MESSAGE[proposition.kind]} but no authority is cited for it. "
                "The output cites elsewhere, so this may be an omission rather than an "
                "invention."
            ),
            output_span=proposition.span,
            evidence=Evidence(
                best_match_text=proposition.text,
                extra={
                    "stage": "L1a",
                    "kind": proposition.kind.value,
                    "cue": proposition.cue,
                    "authority": AuthorityKind.NONE.value,
                    "authorities_in_output": authorities,
                },
            ),
        )

    # -- L1b: citations ------------------------------------------------------------

    def _check_cluster(
        self,
        data: LayerInput,
        cluster: CitationCluster,
        resolution: Resolution | None,
        views: dict[str, _DocumentView | None],
        counts: dict[str, int],
        settings: Settings,
        is_quoted: bool,
    ) -> list[Finding]:
        preferred = cluster.preferred
        label = preferred.raw_text.strip() or f"citation {cluster.ordinal}"

        def make(code: FindingCode, severity: Severity, message: str, evidence: Evidence):
            return Finding(
                id=f"{data.run_id}:L1:cite:{cluster.ordinal}:{code.value}",
                layer=self.layer,
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
                    Evidence(extra={"stage": "L1b", "citation_key": preferred.citation_key}),
                )
            ]

        base_extra = {
            "stage": "L1b",
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
            if not (case_name or is_quoted):
                return findings
            counts["no_document"] += 1
            pending = [
                name
                for name, wanted in (("case name", bool(case_name)), ("quotation", is_quoted))
                if wanted
            ]
            return [
                make(
                    FindingCode.CITATION_UNVERIFIED,
                    Severity.WARN,
                    f"{label} exists, but its text was not available, so the "
                    f"{' and '.join(pending)} attached to it could not be checked.",
                    Evidence(source_url=resolution.url, extra={**base_extra, "pending": pending}),
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

    # -- quotes ---------------------------------------------------------------------

    def _check_quote(
        self,
        data: LayerInput,
        quote: ExtractedQuote,
        by_ordinal: dict[int, CitationCluster],
        views: dict[str, _DocumentView | None],
        counts: dict[str, int],
        scores: list[float],
        settings: Settings,
    ) -> list[Finding]:
        def make(code: FindingCode, severity: Severity, message: str, evidence: Evidence):
            return Finding(
                id=f"{data.run_id}:L1:quote:{quote.ordinal}:{code.value}",
                layer=self.layer,
                code=code,
                severity=severity,
                message=message,
                citation_ordinal=quote.attributed_cluster_ordinal,
                quote_ordinal=quote.ordinal,
                output_span=quote.span,
                evidence=evidence,
            )

        if quote.attributed_cluster_ordinal is None:
            counts["quotes_unattributed"] += 1
            return [
                make(
                    FindingCode.QUOTE_UNATTRIBUTED,
                    Severity.INFO,
                    "This passage is presented as a direct quotation but is not attributed "
                    "to any citation, so there is nothing to check it against.",
                    Evidence(extra={"stage": "L1c", "delimiter": quote.delimiter}),
                )
            ]

        cluster = by_ordinal.get(quote.attributed_cluster_ordinal)
        resolution = _resolution_for(cluster, data.resolutions) if cluster is not None else None
        view = self._view(resolution, data, views)
        if view is None:
            # No document to check against. Not verifying a quote is not evidence that
            # it was invented; the citation-level finding already records why.
            counts["quotes_unverifiable"] += 1
            return []

        needle = normalize(quote.text)
        if len(needle.text) < settings.L1_MIN_QUOTE_CHARS:
            # Short strings match almost anything under partial_ratio, so a score here
            # would be noise dressed as evidence.
            counts["quotes_too_short"] += 1
            return []

        scope_number = quote.pinpoint_paragraph
        pinpoint_hit = False
        haystack_source = view.document.text
        haystack = view.body
        if scope_number is not None:
            found = view.paragraph(scope_number)
            if found is not None:
                # 'at [115]' narrows ~84k characters to one paragraph. That is a large
                # precision win: partial_ratio over a whole judgment will find some
                # window that resembles almost any legal sentence.
                haystack_source, haystack = found
                pinpoint_hit = True

        alignment = fuzz.partial_ratio_alignment(needle.text, haystack.text)
        score = float(alignment.score) if alignment is not None else 0.0
        best_match = (
            haystack.source_slice(haystack_source, alignment.dest_start, alignment.dest_end)
            if alignment is not None
            else None
        )
        counts["quotes_scored"] += 1
        scores.append(score)

        fail_below = settings.L1_QUOTE_FAIL_BELOW
        pass_at = settings.L1_QUOTE_PASS_AT
        if score >= pass_at:
            return []

        paragraph_number = scope_number if pinpoint_hit else view.best_paragraph(needle.text)
        extra = {
            "stage": "L1c",
            "scope": "paragraph" if pinpoint_hit else "document",
            "pinpoint_paragraph": scope_number,
            "pinpoint_found": pinpoint_hit if scope_number is not None else None,
            "delimiter": quote.delimiter,
            "attribution_method": quote.attribution_method.value,
            "quote_chars": len(needle.text),
        }
        threshold = fail_below if score < fail_below else pass_at
        evidence = Evidence(
            score=score,
            threshold=threshold,
            # How far short of the threshold that decided this finding.
            margin=score - threshold,
            best_match_text=best_match,
            best_match_paragraph=paragraph_number,
            source_url=view.document.source_url,
            body_length=len(view.document.text),
            extra=extra,
        )
        if score < fail_below:
            return [
                make(
                    FindingCode.QUOTE_NOT_FOUND,
                    Severity.FAIL,
                    f"This quotation does not appear in the cited judgment "
                    f"(closest match {score:.0f}%, below {fail_below:.0f}%).",
                    evidence,
                )
            ]
        return [
            make(
                FindingCode.QUOTE_INEXACT,
                Severity.WARN,
                f"This quotation is close to the judgment but not verbatim "
                f"({score:.0f}%, below {pass_at:.0f}%). Check the wording before relying "
                "on it.",
                evidence,
            )
        ]

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
