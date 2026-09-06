"""Run L3 over a real judgment and print what it scored, per claim, per config.

    uv run python -m scripts.l3_probe                    # grouped vs paragraph chunks
    uv run python -m scripts.l3_probe --mode heading     # add a prefix arm
    uv run python -m scripts.l3_probe --chunk grouped    # pin one chunk strategy
    uv run python -m scripts.l3_probe --scenario f14     # the answer that opened bug 1
    uv run python -m scripts.l3_probe --answer my.txt    # your own answer text

Arms are the GRID of --mode x --chunk, and each arm reports TWO things.

*The live run* -- L3 through the layer on a real answer, with whatever claims the
splitter produces. This is what the pipeline actually does.

*Separation* -- a fixed calibration set of GENUINE claims (verified present in the
cited judgment, by paragraph) and FOREIGN ones (true Singapore law the judgment does
not decide), scored the way L3 scores: max cos(claim, chunks).

The second exists because the first cannot answer the question on its own. A change
that raises every score looks like an improvement when measured on genuine claims
alone -- floor failures fall -- while buying no discrimination at all, because the
foreign claims rose with them. The number that matters is the GAP: genuine min minus
foreign max. A config that widens it is better; one that raises both equally is not,
however much healthier its pass rate looks.

This is not hypothetical. Paragraph granularity was first reported here as closing a
floor failure (1 of 8 below floor -> 0 of 8) on a genuine-only claim set. Measured
with foreign claims the gap went +0.372 -> +0.354: slightly worse. The floor failure
was real but its cause was elsewhere -- see FRAGMENTS below.

WHY THIS EXISTS. Three separate A/Bs of the contextual prefix have been run against
this pipeline (docs/03-findings.md Part 4, F14) and every one of them was done
out-of-process, by hand, against a reimplementation of the scoring path. That is how
the pipeline came to be measured in a configuration it never actually ran: Part 4's
thresholds were derived on raw paragraphs while the live path embedded prefixed
1,800-token chunks. This script scores through ``SourceGroundingLayer`` itself, so
what it prints is what the layer does.

TWO PROPERTIES THAT MAKE THE ARMS COMPARABLE.

*Each arm gets its own embedding store.* ``sample_background`` keys on MODEL alone, so
arms sharing a repo pollute each other's background pools and the margin -- a
difference of two similarities -- stops meaning anything. A fresh ``InMemoryEmbeddingRepo``
per arm is the cheapest possible isolation.

*Background is built from the same fixed documents in every arm*, under that arm's own
config. Then the only thing varying between arms is the configuration under test,
which is what a controlled comparison requires and what sampling from a shared
database cannot give you.

Runs offline in ``PROVIDER_MODE=mock``: the numbers are then the hashed bag-of-words
model's, not voyage-law-2's, and no threshold transfers between them
(arXiv:2504.16318). Mock mode proves the harness works; a real key produces figures you
can compare against Part 4.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

from verifier.contracts.citations import Resolution
from verifier.contracts.documents import SourceDocument
from verifier.contracts.enums import ResolutionMethod, ResolutionStatus
from verifier.contracts.layers import LayerInput
from verifier.extraction import extract
from verifier.layers.l2_alignment import SourceGroundingLayer
from verifier.providers.factory import get_embedder, get_summariser
from verifier.repos.memory import InMemoryEmbeddingRepo
from verifier.settings import settings
from verifier.sources.elitigation import ElitigationAdapter

CORPUS = Path(__file__).resolve().parent.parent / "tests" / "corpus"

#: The document under test, and the documents that form the contrastive background.
#: The background is deliberately from other areas of law: a pool seeded with the
#: query's own topic collapses every margin and makes correct work look ungrounded.
TARGET = "2007_SGCA_37.html"
BACKGROUND = ("2021_SGHC_100.html",)

MODES = ("none", "heading", "summary_heading")
CHUNKS = ("grouped", "paragraph")

#: The default grid: prefix settled at "none", granularity under test.
DEFAULT_MODES = ("none",)
DEFAULT_CHUNKS = CHUNKS

#: A correct answer about Spandeck: every proposition below is in the judgment, and the
#: citation sits inside the attribution window of each claim. This is the regime that
#: must not fail -- F14's failing claim is the quoted sentence from [115].
DEFAULT_ANSWER = (
    "Singapore applies a single two-stage test for the imposition of a duty of care in "
    "negligence, irrespective of the type of damage claimed: Spandeck Engineering (S) "
    "Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37. The test is premised "
    "on proximity and policy considerations, and is preceded by a preliminary "
    "requirement of factual foreseeability. Legal proximity encompasses physical, "
    "circumstantial and causal proximity, as well as the twin criteria of voluntary "
    "assumption of responsibility and reliance. Policy considerations are applied only "
    "at the second stage, once a prima facie duty of care has been established."
)

DEFAULT_QUESTION = "What is the test for a duty of care in Singapore?"

#: The answer whose L3 failure opened bug 1 (docs/03-findings.md F14): a correct answer
#: citing [2007] SGCA 37 and quoting paragraph [115] verbatim, which L3 failed at 0.325
#: against the 0.35 floor while L1 scored the quote 1.000 and L4 scored 0.751.
#:
#: Reconstructed from F14's description -- the original was a live run and its text was
#: never captured, which is precisely why this one is kept. The verbatim quotation is
#: the load-bearing part: it is a long sentence, and a long sentence is what the claim
#: splitter cut into the fragment that actually caused the failure (F18).
#:
#: It now passes in every configuration tested, INCLUDING the original prefixed and
#: grouped one, with only the splitter fixed. That is the control that shows the prefix
#: and the chunking were neither necessary nor sufficient for the failure.
F14_QUESTION = "What is the test for a duty of care in Singapore?"
F14_ANSWER = (
    "Singapore applies a single two-stage test for the imposition of a duty of care in "
    "negligence, laid down by the Court of Appeal in Spandeck Engineering (S) Pte Ltd v "
    "Defence Science & Technology Agency [2007] SGCA 37. The court summarised its "
    'holding at [115]: "A single test to determine the existence of a duty of care '
    "should be applied regardless of the nature of the damage caused (ie, pure economic "
    "loss or physical damage). It could be that a more restricted approach is preferable "
    "for cases of pure economic loss but this is to be done within the confines of a "
    "single test. This test is a two-stage test, comprising of, first, proximity and, "
    'second, policy considerations."'
)

#: Named inputs, so a regression case is a command rather than a description. Every
#: measurement this pipeline lost track of was one that had been described in prose and
#: never made re-runnable -- three of them surfaced in a single afternoon.
SCENARIOS: dict[str, tuple[str, str]] = {
    "default": (DEFAULT_QUESTION, DEFAULT_ANSWER),
    "f14": (F14_QUESTION, F14_ANSWER),
}

#: GENUINE claims: each is stated by the numbered Spandeck paragraph named beside it,
#: checked against the text rather than remembered. A calibration set whose positives
#: are not actually supported measures nothing.
GENUINE_CLAIMS: tuple[tuple[str, str], ...] = (
    (
        "[81] proximity",
        "Proximity includes physical, circumstantial and causal proximity, and includes "
        "the twin criteria of voluntary assumption of responsibility and reliance.",
    ),
    (
        "[83] policy stage",
        "Policy considerations are applied only at the second stage, once a prima facie "
        "duty of care has been established.",
    ),
    (
        "[115] single test",
        "A single test to determine the existence of a duty of care should be applied "
        "regardless of the nature of the damage caused.",
    ),
)

#: FOREIGN claims: true propositions of Singapore law that this judgment does not
#: decide. Deliberately from other areas -- an easy off-topic negative flatters any
#: configuration, and these are the closest thing to hard negatives the corpus allows.
FOREIGN_CLAIMS: tuple[tuple[str, str], ...] = (
    (
        "sentencing",
        "The court must calibrate the sentence against the applicable "
        "sentencing framework for drug trafficking offences.",
    ),
    (
        "tenancy",
        "A landlord must give reasonable notice before exercising a right of "
        "re-entry under the tenancy agreement.",
    ),
    (
        "illegality",
        "A contract is void for illegality where its performance necessarily "
        "requires the commission of a criminal offence.",
    ),
    (
        "hearsay",
        "Hearsay evidence is inadmissible unless it falls within a recognised statutory exception.",
    ),
)

#: A claim the splitter fragmented, and the sentence it came from. The fragment scored
#: 0.313 and failed the floor; the sentence it was cut out of scores 0.649 in the same
#: configuration. Carried here so the distinction stays measurable rather than becoming
#: a remembered anecdote: an unmatchable proposition the answer never actually made.
FRAGMENTS: tuple[tuple[str, str], ...] = (
    ("fragment", "Policy considerations are applied only at the second stage"),
    (
        "whole sentence",
        "Policy considerations are applied only at the second stage, once "
        "a prima facie duty of care has been established.",
    ),
)


def load(name: str) -> SourceDocument:
    html = (CORPUS / name).read_text(encoding="utf-8", errors="ignore")
    url = f"https://www.elitigation.sg/gd/s/{name.removesuffix('.html')}"
    return ElitigationAdapter().parse(html, url)


def layer_input(question: str, answer: str, document: SourceDocument) -> LayerInput:
    """A LayerInput shaped exactly as the orchestrator builds one.

    Extraction is the real L0 pass, so claim attribution, clustering and the proximity
    window all behave as they do in a run -- a probe that hand-builds its clusters can
    report a score the pipeline would never produce.
    """
    extraction = extract(answer)
    if not extraction.clusters:
        raise SystemExit("no citation found in the answer text; L3 would be NOT_APPLICABLE")
    key = extraction.clusters[0].preferred.citation_key
    return LayerInput(
        run_id="l3-probe",
        question=question,
        ai_output=answer,
        extraction=extraction,
        resolutions={
            key: Resolution(
                citation_key=key,
                status=ResolutionStatus.RESOLVED,
                method=ResolutionMethod.URL,
                url=document.source_url,
                domain="www.elitigation.sg",
                document_id=document.id,
            )
        },
        documents={key: document},
    )


async def seed_background(layer: SourceGroundingLayer, repo: InMemoryEmbeddingRepo) -> int:
    """Embed the background documents under THIS arm's configuration.

    Reuses the layer's own ``_embed_source``, so the background is chunked, prefixed and
    embedded exactly like the document under test. Building it any other way would
    compare a claim against a background produced by different code.
    """
    from verifier.semantic.embed import CachedEmbedder

    embedder = CachedEmbedder(layer._embedder, repo)
    total = 0
    for name in BACKGROUND:
        document = load(name)
        chunks, _ = await layer._embed_source(embedder, document, document.id or name)
        total += len(chunks)
    return total


class SharedSummariser:
    """One claim split and one summary per input, reused by every arm.

    ``Summariser.split_claims`` is a model call and is therefore non-deterministic:
    letting each arm call it separately produced 13, 9 and 8 claims for the same answer.
    Arms that score different claims are not comparable at all, however clean the
    aggregate numbers look -- and the difference is invisible in a mean.

    Memoising also means the LLM tier is exercised exactly as in production (real
    atomic claims, not sentence windows) while costing one call instead of one per arm.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._claims: dict[str, list[str]] = {}
        self._summaries: dict[str, str] = {}

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "shared")

    async def split_claims(self, text: str) -> list[str]:
        if text not in self._claims:
            self._claims[text] = await self._inner.split_claims(text)
        return list(self._claims[text])

    async def summarise_document(self, doc: SourceDocument) -> str:
        key = doc.text_sha256 or doc.source_url
        if key not in self._summaries:
            self._summaries[key] = await self._inner.summarise_document(doc)
        return self._summaries[key]


async def measure_separation(
    layer: SourceGroundingLayer, repo: InMemoryEmbeddingRepo, document: SourceDocument
) -> dict[str, Any]:
    """Score the calibration set against the cited document under this arm's config.

    Deliberately NOT routed through the layer: the layer derives its claims from the
    answer via the splitter, and a controlled comparison needs the same claims in every
    arm. What is computed here is exactly what ``_assess`` consumes -- ``max cos(claim,
    chunks)`` over the same chunks the layer built -- so the numbers are commensurable
    with the live run above them.
    """
    from verifier.semantic.embed import INPUT_TYPE_QUERY, CachedEmbedder
    from verifier.semantic.similarity import top_k

    embedder = CachedEmbedder(layer._embedder, repo)
    chunks, source = await layer._embed_source(embedder, document, document.id or TARGET)

    labelled = [
        *(("genuine", name, text) for name, text in GENUINE_CLAIMS),
        *(("foreign", name, text) for name, text in FOREIGN_CLAIMS),
        *(("fragment", name, text) for name, text in FRAGMENTS),
    ]
    # cache=False: query-side vectors must never enter the pool they are scored
    # against, or a claim is compared with itself.
    result = await embedder.embed_texts(
        [text for _, _, text in labelled], input_type=INPUT_TYPE_QUERY, cache=False
    )

    rows: list[dict[str, Any]] = []
    for (kind, name, _), vector in zip(labelled, result.vectors, strict=True):
        best = top_k(vector, source.vectors, k=1)[0]
        chunk = chunks[best.index]
        rows.append(
            {
                "kind": kind,
                "name": name,
                "score": best.score,
                "where": f"[{chunk.paragraph_from}-{chunk.paragraph_to}]",
            }
        )
    genuine = [r["score"] for r in rows if r["kind"] == "genuine"]
    foreign = [r["score"] for r in rows if r["kind"] == "foreign"]
    return {
        "rows": rows,
        "genuine_min": min(genuine) if genuine else None,
        "foreign_max": max(foreign) if foreign else None,
        "gap": (min(genuine) - max(foreign)) if genuine and foreign else None,
    }


async def run_arm(
    mode: str, chunk: str, question: str, answer: str, summariser: SharedSummariser
) -> dict[str, Any]:
    repo = InMemoryEmbeddingRepo()  # isolation: one pool per arm, never shared
    layer = SourceGroundingLayer(
        embedder=get_embedder(),
        # EVERY arm gets the SAME summariser instance, including the arms that prefix
        # nothing. ``self._summariser`` drives two unrelated things -- the document
        # summary AND ``chunk_output_claims`` -- so withholding it changes the claim
        # set, and calling it per arm re-rolls a non-deterministic split. Sharing one
        # memoised instance fixes both. ``contextual_prefix`` alone decides whether the
        # summary is actually prefixed; see _embed_source.
        summariser=summariser,
        doc_repo=None,
        embedding_repo=repo,
        contextual_prefix=mode,
        chunk_strategy=chunk,
    )
    background_chunks = await seed_background(layer, repo)
    document = load(TARGET)
    result = await layer.run(layer_input(question, answer, document))
    separation = await measure_separation(layer, repo, document)
    return {
        "mode": mode,
        "chunk": chunk,
        "label": f"{mode}/{chunk}",
        "status": result.status.value,
        "score": result.score,
        "background_chunks": background_chunks,
        "detail": result.detail,
        "findings": result.findings,
        "separation": separation,
    }


def report(arm: dict[str, Any]) -> None:
    detail = arm["detail"]
    claims = detail.get("claim_scores") or []
    floor = settings.L2_ABSOLUTE_FLOOR
    fail_at, pass_above = (
        settings.L2_MARGIN_FAIL_AT_OR_BELOW,
        settings.L2_MARGIN_PASS_ABOVE,
    )

    print(f"\n{'=' * 78}\n{arm['label']:<28} status = {arm['status'].upper()}")
    print(
        f"floor {floor:.2f} | margin fail<={fail_at:.2f} pass>{pass_above:.2f} "
        f"| source {detail.get('clusters', [{}])[0].get('source_chunks', '?')} chunks"
        f" | background {arm['background_chunks']}"
    )
    if detail.get("background_empty"):
        print("  ! background empty -- margin skipped, floor only")
    print("-" * 78)

    if not claims:
        print("  no claims assessed:", detail.get("reason") or detail.get("clusters"))
        return

    print(f"  {'s_cited':>8} {'s_bg':>7} {'margin':>8}  claim")
    for entry in claims:
        s_bg = entry["s_background"]
        margin = entry["margin"]
        flag = " FAIL<floor" if entry["s_cited"] < floor else ""
        if not flag and margin is not None:
            flag = (
                " FAIL<margin" if margin <= fail_at else (" warn" if margin <= pass_above else "")
            )
        print(
            f"  {entry['s_cited']:>8.3f} "
            f"{'--' if s_bg is None else format(s_bg, '.3f'):>7} "
            f"{'--' if margin is None else format(margin, '+.3f'):>8}"
            f"{flag}  {' '.join(entry['claim'].split())[:44]}"
        )

    cited = [c["s_cited"] for c in claims]
    below = sum(1 for v in cited if v < floor)
    print("-" * 78)
    print(
        f"  mean s_cited {sum(cited) / len(cited):.3f} | min {min(cited):.3f} "
        f"| below floor {below} of {len(cited)}"
    )

    sep = arm["separation"]
    print("-" * 78)
    print("  separation (fixed calibration set, scored as L3 scores)")
    for row in sep["rows"]:
        mark = {"genuine": "  +", "foreign": "  -", "fragment": "  ?"}[row["kind"]]
        flag = " <floor" if row["score"] < floor else ""
        print(f"  {mark} {row['score']:.3f} {row['where']:>12}  {row['name']}{flag}")
    if sep["gap"] is not None:
        print(
            f"      genuine min {sep['genuine_min']:.3f} | foreign max {sep['foreign_max']:.3f} "
            f"| GAP {sep['gap']:+.3f}   <-- the number that decides"
        )
    print("-" * 78)

    ranked = claims[0].get("ranked_chunks") or []
    if ranked:
        per_claim = detail.get("retrieval", {}).get("passages_per_claim")
        shown = "  ".join(
            f"#{r['rank']}:{r['score']:.3f}[{r['paragraph_from']}-{r['paragraph_to']}]"
            for r in ranked[:6]
        )
        print(f"  claim 1 ranking: {shown}")
        print(f"  (only the top {per_claim} reach the judge)")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, action="append", help="repeatable")
    parser.add_argument("--chunk", choices=CHUNKS, action="append", help="repeatable")
    parser.add_argument(
        "--scenario", choices=sorted(SCENARIOS), default="default", help="built-in input"
    )
    parser.add_argument("--answer", type=Path, help="file with the answer text to score")
    parser.add_argument("--question", default=None)
    args = parser.parse_args()

    question, answer = SCENARIOS[args.scenario]
    if args.answer:
        answer = args.answer.read_text(encoding="utf-8")
    if args.question:
        question = args.question
    modes = args.mode or list(DEFAULT_MODES)
    chunks = args.chunk or list(DEFAULT_CHUNKS)

    print(f"scenario      : {args.scenario}{' (overridden by --answer)' if args.answer else ''}")
    print(f"provider mode : {settings.PROVIDER_MODE}")
    print(f"embeddings    : {settings.EMBEDDINGS_MODEL}")
    if settings.is_mock:
        print(
            "\n  NOTE: mock embedder (hashed bag-of-words). The numbers below are ITS\n"
            "  numbers, not voyage-law-2's, and no threshold transfers between models.\n"
            "  Set PROVIDER_MODE=real with VOYAGE_API_KEY for figures comparable to\n"
            "  docs/03-findings.md Part 4."
        )

    summariser = SharedSummariser(get_summariser())
    arms = []
    for mode in modes:
        for chunk in chunks:
            arms.append(await run_arm(mode, chunk, question, answer, summariser))
            report(arms[-1])

    counts = {len(a["detail"].get("claim_scores") or []) for a in arms}
    if len(counts) > 1:
        # A guard against the confound this script was itself caught by: if the arms
        # scored different numbers of claims, they scored different claims, and the
        # comparison below is meaningless however clean the numbers look.
        print(f"\n  !! arms scored different claim counts {sorted(counts)} -- NOT comparable")

    if len(arms) > 1:
        print(f"\n{'=' * 78}\nCOMPARISON")
        header = (
            f"  {'arm':<24} {'status':<8} {'mean':>7} {'min':>7} {'below floor':>12} {'GAP':>8}"
        )
        print(header)
        for arm in arms:
            claims = arm["detail"].get("claim_scores") or []
            cited = [c["s_cited"] for c in claims]
            if not cited:
                continue
            below = sum(1 for v in cited if v < settings.L2_ABSOLUTE_FLOOR)
            gap = arm["separation"]["gap"]
            print(
                f"  {arm['label']:<24} {arm['status']:<8} {sum(cited) / len(cited):>7.3f} "
                f"{min(cited):>7.3f} {f'{below} of {len(cited)}':>12}"
                f" {'--' if gap is None else format(gap, '+.3f'):>8}"
            )
        print(
            "\n  GAP = genuine min - foreign max, on the fixed calibration set. A config"
            "\n  that raises every score without widening this has bought nothing."
        )

        # The claim that motivated the granularity axis: it clears the contrastive
        # margin comfortably and still fails the absolute floor. Tracking it per arm is
        # more informative than a mean, which averages the one failure away.
        worst = min(
            (c for a in arms for c in (a["detail"].get("claim_scores") or [])),
            key=lambda c: c["s_cited"],
            default=None,
        )
        if worst is not None:
            print(f"\n  worst claim: {' '.join(worst['claim'].split())[:62]!r}")
            print(f"  {'arm':<24} {'s_cited':>8} {'margin':>8}  {'rank of best chunk':>20}")
            for arm in arms:
                match = next(
                    (
                        c
                        for c in (arm["detail"].get("claim_scores") or [])
                        if c["claim"] == worst["claim"]
                    ),
                    None,
                )
                if match is None:
                    continue
                top = (match.get("ranked_chunks") or [{}])[0]
                where = f"[{top.get('paragraph_from')}-{top.get('paragraph_to')}]"
                margin = match["margin"]
                print(
                    f"  {arm['label']:<24} {match['s_cited']:>8.3f} "
                    f"{'--' if margin is None else format(margin, '+.3f'):>8}  {where:>20}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
