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
   ├─ L0 extract citations, quotes, propositions, explicit source URLs  (sync, <50 ms)
   │     ├─ L1a: output asserts law and cites NOTHING → FAIL now.
   │     │       No fetch, no worker, no tokens. ~5 ms.
   │     └─ L2a: blacklisted domain named outright → FAIL now.
   │             No fetch, no worker, no tokens. ~5 ms.
   │
   └─ enqueue ─────────────────────────────────────────► 202 {run_id} in ~5 ms

        parallel from t=0                                  GATE            final
        ┌ L1b/c resolve → existence + quote ┬─► L2b source trust ┐
        │      (shared single-flight        │   (needs L1b's     │
        │       document fetch)             │   resolved domain) ├─► any L1-L4 FAIL?
        ├ L3 grounding (shares the fetch) ──┘                    │   ├ yes → FAIL, no judge
        └ L4 responsiveness (no deps) ───────────────────────────┘   └ no  → L5 judge
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
Fabricating no citations is not the same as citing correctly. L1a is pure text, so it
reaches its verdict in the same ~5 ms budget as the L2a blacklist check: no fetch, no
worker, no tokens.

**L2 follows L1** because a bare citation like `[2007] SGCA 37` has no source until L1
resolves it to a document on a domain. L2 asks "is this source trustworthy?", which
cannot be answered before there is a source. Domains written out explicitly in the
output are the exception — they carry a domain already, so L2a checks them at
extraction time and can fail the run before anything is fetched.

**L3 does not wait for L1's verdict.** It needs the *fetched document*, not L1's
opinion of it, so both consume one shared single-flight `resolve_document()` — one
fetch, two consumers, both proceeding the moment it lands. We are deliberately
optimistic: L3 and L4 score the output regardless of how L1 rules.

The reason is a product one. **A citation can be fabricated while the legal argument is
sound, and a lawyer needs to know that.** "Citation is fabricated, but the proposition
is well-grounded and does answer your question" is far more useful than "failed at
layer 1". The verdict is still FAIL; the *report* is complete.

This costs almost nothing: when a citation does not resolve, L3 returns
`NOT_APPLICABLE` and spends no embeddings, so only L4 runs. And because the layers are
parallel, a fabricated-citation red is bounded by `max(L1, L4)` ≈ 0.6 s.

### Concurrency

`asyncio.gather` **within** one Celery task for the layers; Celery for run-level
concurrency. Every layer is I/O-bound, so nested chords buy no wall-clock, add a Redis
round-trip per layer, and chord-in-chord-body is Celery's flakiest corner. Celery earns
its place elsewhere: `/v1/verify` returns in ~5 ms, many *runs* execute concurrently
(the "thousands of queries daily" requirement), and the slow Opus and browser calls sit
on isolated queues where they cannot starve the fast path.

## The verdict model

- **FAIL** — any L1–L4 failure: **nothing cited at all** · citation not found ·
  soft-404 · title or party mismatch · quote not found · blacklisted source · claim not
  grounded · question not answered. The judge is skipped.
- **WARN** — passes, annotated: an individual assertion with no citation in scope ·
  nothing cited on a *follow-up* turn · graylisted source · inexact-but-close quote ·
  `UNVERIFIED` citation (report-only, source unavailable, session expired).
- **PASS** — clear; the judge then runs and returns the final verdict.

### "Cannot verify" is never "fabricated"

Only positive evidence of non-existence may fail a run. A report-only citation, an
expired login-walled session, and a source outage all produce WARN. This is not
politeness — it is the difference between a tool lawyers trust and one they turn off.
See `docs/03-findings.md` F12 for the near-miss that made this concrete.

**But WARN must not be allowed to read as "fine."** A run in which no citation could be
checked used to present as a mild warn with a grey `not_applicable` beside L3 — both
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
either direction. `L3 NOT_APPLICABLE` means the layer received no document to score —
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
| L1a | Is the proposition supported by any authority at all? | Deterministic count |
| L1b | Does this citation exist, and is it the right document? | Deterministic lookup |
| L1c | Is the quote really in it? | Deterministic lookup |
| L2 | Is the source trustworthy? | Deterministic lookup |
| L3 | Does the output *use* this source? | Retrieval / ranking |
| L4 | Does the output answer *this* question? | Retrieval / ranking |
| L5 | Is it *faithful* to what the source holds? | Reasoning |

## Latency

| Path | Deterministic verdict | Final |
|---|---|---|
| Blacklisted URL named in the output | ~5 ms | ~5 ms |
| Output asserts law and cites nothing | ~5 ms | ~5 ms (judge skipped) |
| Fabricated citation | ~0.6 s = `max(L1, L4)` | ~0.6 s (judge skipped) |
| Cold pass, open source | ~4 s | ~12–19 s |
| Cold pass, login-walled source | ~7–9 s | ~15–24 s |
| Warm pass (cached doc + summary + embeddings) | ~0.15 s | ~6–15 s |

## Scalability

Every expensive artefact is content-hash keyed, so the second query touching a given
case pays nothing: `citation_resolutions` skips the fetch (including an expensive
browser fetch), `document_summaries` skips a Sonnet call, `text_embeddings` skips ~50
Voyage calls. A deterministic failure costs **zero** LLM tokens. Singapore appellate
law has a heavy head, so hit rates should exceed 90% after warm-up. Runs report
`cache_hits`/`cache_misses` so the claim is measured rather than asserted.
