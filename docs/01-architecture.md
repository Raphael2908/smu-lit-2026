# Architecture

## The input

The Chrome extension scrapes two things off the claude.ai page: **the user's prompt**
and **Claude's rendered output**. That pair is the entire input. There is no API
integration with the model under test, no access to its retrieval context, logprobs or
internals.

Two consequences:

- **Model- and vendor-agnostic.** Anything that produces a `(question, answer)` pair
  can be audited — Claude, a competitor, or SAL's own database AI. That is what makes
  this an evaluation *framework* rather than a feature of one product, and it is
  central to the "who audits the auditor" story: the auditor needs no privileged
  access to the thing it audits.
- **Verification is post-hoc and text-only.** Everything must be recoverable from
  rendered text, which is why L1 re-resolves every citation from scratch rather than
  trusting any metadata.

## The DAG

```
POST /v1/verify {question, ai_output, context, is_followup}
   │
   ├─ L0 Haiku finds the citations; regex extracts quotes, propositions, source URLs
   │     ├─ L1a: output asserts law and cites NOTHING → FAIL now.
   │     │       No fetch, no worker. ~5 ms deterministic, ~1 s with the LLM extractor.
   │     └─ L1c, pre-fetch: blacklisted domain named outright → FAIL now.
   │             No fetch, no worker, no tokens. ~5 ms.
   │
   └─ enqueue ─────────────────────────────────────────► 202 {run_id} in ~5 ms

        parallel from t=0                                GATE              final
        ┌ L1  1a cited at all ──► 1b resolve ──► 1c trust ┐
        │     (shared single-flight document fetch;       │
        │      1c needs 1b's resolved domain, which is    ├─► any L1-L3 FAIL?
        │      why it is sequenced INSIDE L1)             │   ├ yes → FAIL, no judge
        ├ L2 alignment (shares the fetch) ────────────────┤   └ no  → L4 judge
        └ L3 responsiveness (no deps) ────────────────────┘
```

### Why this ordering

Citation forms follow the sponsor's house style, the SAL *SLR Style Guide* (2021) —
neutral citations, both bracket conventions for law reports, the four statute forms, and
the subsequent-reference shorthand (`("ANJ")`, `([1] supra)`) that SLR style makes
pervasive. That guide is a specification here, not a nicety: a form we fail to recognise
is authority the answer gets no credit for. See `docs/03-findings.md` F13a.

**L1a comes first, before anything is fetched.** L1b and L1c only ever examine
authority the output actually wrote down, so an answer that states the law from memory
— confidently, fluently, citing nothing — passes them both without a mark against it.
Fabricating no citations is not the same as citing correctly. L1a needs no fetch and no
worker: it reaches its verdict from the text alone.

**Finding a citation is a recognition problem; typing one is a parsing problem.** A
regex can only recognise the forms someone enumerated, and every form it misses is
authority the answer gets no credit for — six such gaps were found against the SLR Style
Guide (F13) and each was a false red waiting to happen. So Haiku reads the answer and
says which spans are citations, and `extraction/citations.py` decides what each one
actually is. The model never types a citation: `build_url` trusts `CitationType.NEUTRAL`
without checking the court is Singaporean, so a model-typed `[2019] UKSC 32` would be
fetched from eLitigation and its soft-404 read as proof the case does not exist.

Two consequences follow, and both are load-bearing. Every candidate must be found
**verbatim** in the answer before it counts, so a citation the model invented can never
supply the authority that clears the FAIL. And when the extractor does not run at all —
timeout, no key, unreadable output — the run carries `extractor_degraded` and L1a
declines to fail: "we did not look" is not "it cited nothing". There is deliberately no
regex fallback, because falling back would report a run as checked when it was not.

**1c is sequenced last inside L1**, because a bare citation like `[2007] SGCA 37` has
no source until 1b resolves it to a document on a domain. "Is this source trustworthy?"
cannot be answered before there is a source. Domains written out explicitly in the
output are the exception — they carry a domain already, so 1c runs a pre-fetch pass over
those at extraction time and can fail the run before anything is fetched.

That data dependency is *why* trust is a sub-check rather than a layer. As a layer it
had to wait for the whole of L1 to finish, which gave the pipeline a sequential tail for
a check that only ever needed one field. Inside L1 the dependency sits where it actually
lives, and every deterministic layer now starts at t=0.

**A whitelist can never clear a 1b finding.** Merging the two brought that guarantee
into one object, so it is enforced there rather than promised: `check_source_trust`
receives a `LayerInput` and nothing else — no findings, no other sub-check's result — so
it is not *capable* of reading a fabrication finding, let alone clearing one. The
composite only concatenates, and a tripwire raises `ContractViolation` if a future edit
ever drops a sub-check's findings on the way out. If "whitelisted overrules all" were
implemented literally, putting elitigation.sg on the whitelist would clear every
fabricated eLitigation citation in existence.

**L2 does not wait for L1's verdict.** It needs the *fetched document*, not L1's
opinion of it, so both consume one shared single-flight `resolve_document()` — one
fetch, two consumers, both proceeding the moment it lands. We are deliberately
optimistic: L2 and L3 score the output regardless of how L1 rules.

The reason is a product one. **A citation can be fabricated while the legal argument is
sound, and a lawyer needs to know that.** "Citation is fabricated, but the proposition
is well-grounded and does answer your question" is far more useful than "failed at
layer 1". The verdict is still FAIL; the *report* is complete.

This costs almost nothing: when a citation does not resolve, L2 returns
`NOT_APPLICABLE` and spends no embeddings, so only L3 runs. And because the layers are
parallel, a fabricated-citation red is bounded by `max(L1, L3)` ≈ 0.6 s.

### Concurrency

`asyncio.gather` **within** one Celery task for the layers; Celery for run-level
concurrency. Every layer is I/O-bound, so nested chords buy no wall-clock, add a Redis
round-trip per layer, and chord-in-chord-body is Celery's flakiest corner. Celery earns
its place elsewhere: `/v1/verify` returns in ~5 ms, many *runs* execute concurrently
(the "thousands of queries daily" requirement), and the slow Opus and browser calls sit
on isolated queues where they cannot starve the fast path.

## The verdict model

- **FAIL** — any L1–L3 failure: **nothing cited at all** · citation not found ·
  soft-404 · title or party mismatch · blacklisted source · claim not grounded ·
  question not answered. The judge is skipped.
- **WARN** — passes, annotated: an individual assertion with no citation in scope ·
  nothing cited on a *follow-up* turn · graylisted source ·
  `UNVERIFIED` citation (report-only, source unavailable, session expired).
- **PASS** — clear; the judge then runs and returns the final verdict.

### "Cannot verify" is never "fabricated"

Only positive evidence of non-existence may fail a run. A report-only citation, an
expired login-walled session, and a source outage all produce WARN. This is not
politeness — it is the difference between a tool lawyers trust and one they turn off.
See `docs/03-findings.md` F12 for the near-miss that made this concrete.

**But WARN must not be allowed to read as "fine."** A run in which no citation could be
checked used to present as a mild warn with a grey `not_applicable` beside L2 — both
statements true, and together misleading, because "we found small problems" and "we
verified nothing at all" looked identical at a glance. The verdict is unchanged; the
*report* now carries the weight:

| Situation | Verdict | What the panel leads with |
|---|---|---|
| Source reachable, no such judgment | **FAIL** | **Fabricated citation**, named in full |
| Source unreachable / report-only / session expired | WARN | **Nothing was verified** — `0 of N` checked, each one named with why |
| Some checked, some not | WARN | `M of N verified`, the rest named |

The unverified state is given its own label and colour rather than a second shade of
warn, because it is a different fact: not a small problem, but an absence of evidence in
either direction. `L2 NOT_APPLICABLE` means the layer received no document to score —
it is not a failed check and must never be aggregated as one.

## The invariant

```python
def should_run_judge(det_verdict, options):
    return det_verdict is not Verdict.FAIL      # fail-fast: the only short-circuit

def finalize(det_verdict, det_findings, judge):
    added = judge.findings                       # the judge may only ADD
    final = lattice_min(det_verdict, judge.verdict)   # monotone: only moves down
    assert VERDICT_ORDER[final] <= VERDICT_ORDER[det_verdict]
```

**The LLM can convict but never acquit.** It only ever sees output that already passed
every deterministic check; its response schema has no field capable of clearing a
finding; and the aggregator would ignore one if it had. `Finding.source` marks each
finding `deterministic` or `llm`, and the extension renders them differently — the
invariant made visible rather than merely asserted.

`tests/pipeline/test_judge_cannot_launder.py` drives a mock judge returning "pass"
against a fabricated-citation run and asserts the judge was never invoked and the
verdict is still FAIL. **That test is the answer to "who audits the auditor?"**

## Which layer does which job

Each layer attempts only the task its tool is proven for. Published results show
embedding similarity cannot detect real LLM hallucinations, so nothing here asks
cosine to decide truth — see `docs/03-findings.md` Part 2.

| Layer | Question | Task type |
|---|---|---|
| L1a | Is the proposition supported by any authority at all? | Model finds, deterministic count |
| L1b | Does this citation exist, and is it the right document? | Deterministic lookup |
| L1c | Is the source trustworthy? | Deterministic lookup |
| L2 | Does the output *use* this source? | Retrieval / ranking |
| L3 | Does the output answer *this* question? | Retrieval / ranking |
| L4 | Is it *faithful* to what the source holds? | Reasoning |

L1a/L1b/L1c are SUB-CHECKS, not layers: they are parts of one question — "is the citation
integrity of this answer sound?" — and the run reports them on `LayerResult.sub_results` inside
a single L1 row. Four scoring layers, four rows.

## Latency

| Path | Deterministic verdict | Final |
|---|---|---|
| Blacklisted URL named in the output | ~5 ms | ~5 ms |
| Output asserts law and cites nothing | ~5 ms + one Haiku call | same (judge skipped) |
| Fabricated citation | ~0.6 s = `max(L1, L3)` | ~0.6 s (judge skipped) |
| Cold pass, open source | ~4 s | ~12–19 s |
| Cold pass, login-walled source | ~7–9 s | ~15–24 s |
| Warm pass (cached doc + summary + embeddings) | ~0.15 s | ~6–15 s |

## Scalability

Every expensive artefact is content-hash keyed, so the second query touching a given
case pays nothing: `citation_resolutions` skips the fetch (including an expensive
browser fetch), `document_summaries` skips a Sonnet call, `text_embeddings` skips ~50
Voyage calls. A deterministic failure costs no judge and no embedding tokens; since L1a
began asking a model which citations an answer offered it does cost one Haiku call, which
is not yet cached — see `todo.md`. Singapore appellate
law has a heavy head, so hit rates should exceed 90% after warm-up. Runs report
`cache_hits`/`cache_misses` so the claim is measured rather than asserted.
