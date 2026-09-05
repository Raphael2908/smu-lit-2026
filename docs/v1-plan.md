<!--
  The v1 implementation plan, as approved before the build began.

  Kept verbatim as a record of what was decided and why. Where the build has since
  diverged, docs/01-architecture.md and docs/03-findings.md are authoritative -- see
  "What changed during the build" at the foot of this file.
-->

# SAL Verifier — 5-Layer Legal AI Output Verification

## Context

**Problem (SMU LIT 2026, sponsor: Singapore Academy of Law).** LLMs fabricate case law and misread precedent; subscribers to a case-law database demand absolute accuracy. Build an automated, scalable framework that scores the accuracy and citation integrity of legal AI outputs, and answers "who audits the auditor?".

**Solution.** A verification service running five layers over `(question, ai_output)`. Layers 1–4 are deterministic and run in parallel; **if any of them fails, the output fails immediately and the LLM judge is never consulted.** The judge runs only on output that already passed every deterministic check, as a final correctness arbiter.

**The input comes entirely from the Chrome extension**, which scrapes two things off the claude.ai page: **the user's prompt** and **Claude's rendered output**. That pair is the whole input — there is no API integration, no access to Claude's retrieval context, logprobs, or internals. Two consequences, one good and one to design around:

- **It is model- and vendor-agnostic.** Anything that can produce a `(question, answer)` pair can be audited by this system — Claude, a competitor, or SAL's own database AI. That is what makes it an *evaluation framework* rather than a feature of one product, and it is central to the "who audits the auditor" story: the auditor doesn't need privileged access to what it audits.
- **Verification is post-hoc and text-only.** Everything must be recoverable from rendered text, which is why L1 re-resolves citations from scratch rather than trusting any metadata, and why the extension's capture contract (below) is load-bearing rather than cosmetic.

**How it answers the brief:**
- *Fake cases* → L1 resolves every citation against the live source corpus. Non-resolution is ground truth, not an opinion.
- *Contextual accuracy* → split across two task types on purpose. L3/L4 use legal-domain embeddings as **retrieval**: does the output actually draw on the cited source, and does it answer *this* question? L5 then does the **reasoning** part — is it faithful to what the case holds. Published results show embedding similarity cannot judge faithfulness; the architecture reflects that rather than ignoring it.
- *Scalability* → content-hash caches (document, summary, embeddings) make the warm path sub-second, and a deterministic failure costs no LLM tokens at all.
- *Who audits the auditor* → **the LLM never gets to clear a failure.** It only ever sees output that passed all four deterministic layers, and it can only fail it further. It can convict, never acquit.

### Scope decided with the user
Progressive delivery + short-circuit · Singapore corpus (eLitigation open; LawNet behind a login wall) · **no** bias layer · **no** benchmark harness — the architectural rule, made executable as one test, is the "who audits the auditor" answer.

> *If judges ask about the omissions:* bias would attach as an L5 rubric dimension plus an authority-balance signal — the citation graph it needs is already free (F5). Measuring the verifier itself would be a labelled poisoned-citation set scored for precision/recall. Both are additive; neither changes the architecture.

---

## Verified by live probing — do not re-derive

| # | Finding | Evidence |
|---|---|---|
| F1 | Neutral citation → deterministic URL | `[2007] SGCA 37` → `/gd/s/2007_SGCA_37`; confirmed SGCA + SGHC |
| F2 | eLitigation judgments are **static HTML, no JS** | 115,404 chars of full text in **0.26 s** over plain `curl` |
| F3 | **Fake citations return HTTP 200**, not 404 | Soft-404: real **150,389** bytes vs fake **3,549**; fake has empty `<title>` + "Page Not Found" |
| F4 | Right-case confirmation is free | Real page has `<title>[2007] SGCA 37</title>`; party names appear verbatim |
| F5 | Judgment HTML is **cleanly structured** | `Judg-1` ×150 (numbered paragraphs), `Judg-Quote-1` ×49, `Judg-Heading-1/2/3` ×8/17/22, `<nobr>` wrapping parallel citations → free chunking, heading hierarchy, pinpoint lookup, citation graph |
| F6 | Name search works, and is binary | GET `/gd/Home/Index?SearchPhrase=…&Filter=SUPCT&SearchMode=True` → `Tan Cheng Bock v AG` returns `2017_SGCA_50` at **rank 1**; two fabricated case names returned **0 hits** each |
| F7 | **Report citations do NOT resolve** | `"[2007] 4 SLR(R) 100"` → 10 hits, none of them Spandeck. The index is full-text, so it finds cases that *cite* the report, not the case |
| F8 | **Naive Ctrl+F is actively wrong** | verbatim `in`=True/fuzzy 0.870 · one word changed `in`=**False**/fuzzy **0.869** · paraphrase 0.267 · fabrication 0.483 |
| F9 | A judgment **does not fit** one embedding call | Body 83,808 chars ≈ **21K tokens** vs voyage-law-2's 16K context |
| F10 | `SGHC(A)` → `SGHCA` in the URL | Observed `/gd/s/2022_SGHCA_26`; parens stripped, not encoded |
| F11 | Models / infra | `voyage-law-2` live (16K ctx, 1024 dims); IDs are `claude-sonnet-5` / `claude-opus-5`; local Python **3.14** (need 3.12), **Docker daemon not running**, **no vendor keys set**; `gh` authed as `Raphael2908` |

### Four corrections to the original design — each a correctness issue, not a preference

1. **Two fetch strategies, routed by source.** Open sources (eLitigation) go over plain HTTP at 0.26 s — a browser there would add seconds for nothing. **Login-walled sources (LawNet, the sponsor's own product) need the headless browser**, with a persistent authenticated profile. Fetching is a `Fetcher` protocol with `HttpFetcher` and `BrowserFetcher` impls, chosen per domain by the source registry. Fast path stays fast; pay browser cost only where required.
2. **Fuzzy, not Ctrl+F (F8).** A one-word drift breaks exact matching while fuzzy stays at 0.869 — naive Ctrl+F would falsely accuse correct output of fabrication.
3. **L1 must run on quoted text only (F8).** A genuine paraphrase scores **lower** (0.267) than a fabrication (0.483) — lexical matching is *anti-correlated* on paraphrase. Paraphrased attributions are L3's job. Enforce in the type system: `ExtractedQuote` requires a quote-delimiter provenance field.
4. **"Cannot verify" is never "fabricated."** A report-only citation (F7) and an unreachable login-walled source both yield `UNVERIFIED` — a WARN that passes, not a FAIL. Claiming fabrication on correct input is the worst error this product can make.

---

## Architecture

**Run L1, L3 and L4 optimistically in parallel; L2 follows L1.** Two things drive this:

- **L2 depends on L1.** L2 asks "is this source trustworthy?", but a bare citation like `[2007] SGCA 37` has *no source* until L1 resolves it to a document on a domain. L2 can only judge a resolved citation, so it runs after L1. (Explicit URLs written in the output are the exception — those carry a domain already and get checked instantly at L0.)
- **L3 and L4 do not wait on L1's verdict.** L3 needs the *fetched document*, not L1's opinion of it, so both consume one shared single-flight `resolve_document(citation)` — the fetch happens once and both proceed the moment it lands. **We assume optimistically that citations are valid** and score grounding and responsiveness regardless of how L1 rules.

The product reason is the important one: **a citation can be fabricated while the legal argument is sound, and a lawyer needs to know that.** "Citation is fabricated, but the proposition is well-grounded and does answer your question" is far more useful than "failed at layer 1." The verdict is still FAIL; the *report* is complete.

```
POST /api/verify {question, ai_output}
   │
   ├─ L0 extract citations, quotes, explicit source URLs (sync, <50 ms), create run
   │     └─ blacklisted URL named outright → FAIL now: no fetch, no worker, no tokens
   │
   └─ enqueue ───────────────────────────────────► return {run_id} at once (~5 ms)

        parallel from t=0                              GATE              final
        ┌ L1 resolve → existence + quote ─┬─► L2 source trust ─┐
        │      (shared single-flight       │   (needs L1's       │
        │       document fetch)            │    resolved domain) │
        ├ L3 grounding ───────────────────┘                     ├─► any L1-L4 FAIL?
        └ L4 responsiveness (no deps)                           │   ├ yes → FAIL, no judge
                                                                 └   └ no  → L5 judge
```

**Fail-fast remains the gate.** If any of L1–L4 fails, the run fails and the judge is never called — the judge only ever sees output that passed every deterministic check, which makes "the judge cannot launder a failure" structural rather than merely enforced. But all four layers **run to completion** before the verdict is emitted, so the user gets every reason at once instead of one per re-run.

This costs almost nothing. On a fabricated citation the document never resolves, so L3 returns `NOT_APPLICABLE` and spends no embeddings; only L4 runs (~1 cached-or-cheap Voyage call). And because the layers are parallel, the fabricated-citation red is bounded by `max(L1, L4)` ≈ L1 ≈ **0.6 s** — no latency regression versus the old serial ordering.

*Future hook:* when a citation is fabricated but the proposition looks sound, the natural next step is to search the corpus for **any** real authority supporting it — turning "this citation is fake" into "this citation is fake, but [2007] SGCA 37 does say this." Out of scope now; the search path (F6) already exists.

**Concurrency:** `asyncio.gather` *within* one Celery task for the layers; Celery for run-level concurrency. Every layer is I/O-bound, so nested chords buy zero wall-clock, add a Redis round-trip per layer, and chord-in-chord-body is Celery's flakiest corner. Celery earns its place elsewhere: `/verify` returns in ~5 ms, many *runs* run concurrently (the "thousands of queries daily" requirement), and the slow Opus and browser calls sit on their own queues where they cannot starve the fast path.

### Verdict model

- **FAIL** — any L1–L4 failure: citation not found · soft-404 · title or party mismatch · quote not found · **blacklisted source** · alignment below threshold · doesn't answer the question. → judge skipped.
- **WARN** — passes, annotated: graylisted source · inexact-but-close quote · `UNVERIFIED` citation (report-only, or login-walled source unreachable).
- **PASS** — all clear; the judge then runs and returns the final verdict.

```python
VERDICT_ORDER = {PASS: 2, WARN: 1, FAIL: 0}          # lattice; lower is worse

def deterministic_verdict(findings):
    if any(f.severity is FAIL for f in findings): return FAIL
    if any(f.severity is WARN for f in findings): return WARN
    return PASS

def should_run_judge(det_verdict, opts):
    if opts.force_judge: return True                  # debug/eval escape hatch only
    return det_verdict is not FAIL                    # ← fail-fast: any L1-L4 failure skips L5

def finalize(det_verdict, det_findings, judge):
    if judge is None:
        return det_verdict, det_findings, "deterministic"
    # THE INVARIANT: the judge may only ADD findings. Its output schema has no
    # field capable of clearing one, and any such field would be ignored.
    added = [f for f in judge.findings]
    final = min(det_verdict, judge.verdict, key=VERDICT_ORDER.get)   # monotone
    assert VERDICT_ORDER[final] <= VERDICT_ORDER[det_verdict], "judge tried to upgrade"
    return final, det_findings + added, "final"
```

`tests/pipeline/test_judge_cannot_launder.py` drives a mock judge returning `PASS` ("the citation is fine actually") against a fabricated-citation fixture, and asserts the judge was **never invoked** and the verdict is still `FAIL` with the L1 finding intact. **That test is the deliverable answer to "who audits the auditor?"** — the LLM is a jury, not a judge.

### Latency budget

| Path | Deterministic verdict | Final |
|---|---|---|
| Blacklisted URL named in the output (L2a) | **~5 ms** | ~5 ms (no fetch, no worker) |
| Fabricated citation | **~0.6 s** = `max(L1, L4)` | ~0.6 s (judge skipped) |
| Cold pass, open source | ~4 s | ~12–19 s |
| Cold pass, login-walled source | ~7–9 s | ~15–24 s |
| Warm pass (cached doc + summary + embeddings) | **~0.15 s** | ~6–15 s |

---

## Layers

**L0 — extraction** (`extraction/`). Neutral-citation regex with courts *enumerated* (`SGCA|SGCA\(I\)|SGHC|SGHC\(I\)|SGHC\(A\)|SGHCF|SGHCR|SGDC|SGMC|SGFC`), never `[A-Z]+`, or it matches acronyms. Normalize to `court:year:num`; strip parens for the URL (F10). Also extract report citations, case names, and **any bare URLs / source references** in the output — those feed L2. Case-name parsing favours precision (≥2 Title-Case tokens each side of ` v `): a bad parse causes a spurious 0-hit search and a false fabrication claim.

**Clustering matters.** A reference usually arrives as `Spandeck … v DSTA [2007] 4 SLR(R) 100; [2007] SGCA 37`. Cluster name + report cite + neutral cite within ~80 chars into one resolution attempt, preferring **neutral → name → report**. This is what rescues report citations from F7: in practice they travel with a resolvable sibling.

**Quote attribution**, in priority order: (1) **pinpoint** — `at \[(\d+)\]` → attribute *and record the paragraph*, so verification runs against one paragraph rather than 83K chars, a large precision win for `partial_ratio`; (2) explicit, same sentence; (3) proximity, same paragraph ≤400 chars; (4) none → INFO, never a fail.

**L1 — citation existence + quote verification** (`layers/l1_existence.py`). Cache lookup → neutral URL, else name search (F6), else `UNVERIFIED` (F7). Soft-404 needs all three signals to agree (`len < 10_000` **and** empty first `<title>` **and** "Page Not Found") — disagreement is `AMBIGUOUS`, a WARN. Then confirm `<title>` equals the requested citation, and cross-check party names — this catches the nastiest class, a *real* citation attached to the *wrong* case name. Quote check: normalize (NFKD, curly→straight quotes, dashes, whitespace, casefold — each alone breaks exact matching), scope to the pinpointed paragraph when known, `rapidfuzz.fuzz.partial_ratio`. Skip quotes <40 chars; short strings match anything.

**L2 — source trust lists** (`layers/l2_lists.py`). **This layer is about *where the material comes from*, not whether a citation exists** — which is why it runs **after L1**: a bare citation has no domain until L1 resolves it. Two phases:

- **L2a, inline at L0 (~5 ms).** Domains named outright in the output (bare URLs, "according to lawgurublog.com"). A blacklist hit here fails immediately with no fetch, no worker, and no tokens.
- **L2b, after L1.** Every *resolved* citation's domain, taken from L1's resolution.

Every source maps to a domain or publisher (`elitigation.sg`, `lawnet.sg`, `sso.agc.gov.sg`, or a random blog / content farm / AI-generated case site).

- **Whitelist** — authoritative sources (eLitigation, LawNet, AGC SSO, SAL publications). Overrules: accepted without further source-level scrutiny; suppresses graylist and heuristic source warnings.
- **Graylist** — passes with a note in the output (secondary commentary, foreign aggregators, Wikipedia).
- **Blacklist** — untrusted source → **immediate FAIL**, evaluated *before* any fetch or worker dispatch. ~5 ms, zero network, zero tokens.

`list_entries(list_type, match_type, pattern, reason, active)` with `match_type ∈ {domain, url_pattern, publisher}`; domain matching covers subdomains. Seed ~30 entries. The lists are user-maintained via `/v1/lists`.

> Note the clean separation: L2 answers *"is this source trustworthy?"*, L1 answers *"does this citation actually exist there?"*. Whitelisting `elitigation.sg` does not assert that `[2019] SGCA 214` exists on it — so trusting a source can never launder a fabricated citation. The two layers ask different questions and both must pass.

**L3 — source grounding** (`layers/l3_alignment.py`). **The question is "does the output actually use this source?", not "is this claim true?"** That distinction is what makes cosine the right tool: grounding is a *retrieval* problem — if the output were produced by a RAG system over this document, its content would land in the top-k chunks. Retrieval is a **ranking** task, and ranking is precisely the property that survives anisotropy ([arXiv:2601.16907](https://arxiv.org/abs/2601.16907)); factual faithfulness is L5's job, and the literature says only a reasoning judge can do it.

L3 consumes the **same single-flight `resolve_document()`** as L1, so the two run concurrently over one fetch and L3 never waits on L1's verdict. Pipeline: claim-chunk the output via Sonnet (`claim_split` → JSON array), falling back to 2-sentence windows on parse failure or in mock mode. Chunk the source using `Judg-1`/`Judg-Quote-1` paragraphs (F5), merged to ~1,800 tokens — chunking is **mandatory**, not an optimisation (F9). Summarise each document once with `claude-sonnet-5` (~250 tokens: court, parties, issue, holding, ratio), cached by `(text_sha256, model, prompt_version)`. Prefix `summary + heading path` onto every chunk before embedding, then embed with `voyage-law-2` (1024 dims, `input_type="document"`; claims are `input_type="query"`).

Scoring is **contrastive, not absolute** (see Thresholds):

```python
s_cited = max(cos(claim, c) for c in chunks_of_cited_doc)
s_bg    = max(cos(claim, c) for c in BACKGROUND)   # ~200 chunks from other cached judgments
margin  = s_cited - s_bg
```

If the cited judgment supports the claim no better than a random unrelated judgment does, the output is not grounded in it → FAIL `CLAIM_NOT_GROUNDED_IN_SOURCE`. Report the best-matching passage as evidence either way — that is what the panel shows the user. If the citation never resolved, L3 is `NOT_APPLICABLE`.

> Keep `voyage-law-2` as default. The `EmbeddingsProvider` interface also exposes `contextualized_embed()`, so `EMBEDDINGS_MODEL=voyage-context-4` switches to Voyage's native contextual endpoint and skips the manual prefix — a one-env-var A/B.

**L4 — responsiveness** (`layers/l4_responsiveness.py`). Independent, starts at t=0, no LLM call. **Does the output answer the user's question?** Straight cosine — embed the question as it arrived, embed the answer's chunks, take the max:

```python
score = max(cos(question, c) for c in output_chunks)     # FAIL < 0.50 · WARN 0.50-0.70 · PASS >= 0.70
```

Three implementation details that decide whether the number means anything:

- **Use Voyage's `input_type` asymmetry.** Question with `input_type="query"`, answer chunks with `input_type="document"`. Voyage applies different internal prompts per type, mapping a short interrogative and a long declarative passage into the same retrieval space. Skip this and a *perfect* answer scores artificially low purely from length and register mismatch.
- **Max over chunks, not the whole answer.** Embedding a 600-word answer as one vector dilutes the responsive part into surrounding analysis.
- **Guard short answers.** Under ~20 tokens ("Yes.", "It depends.") scores erratically → WARN rather than score.

*Known blind spot:* a two-part question answered only in part still scores well on max-over-chunks. That is L5's `responsiveness` dimension, and L5 sees exactly these outputs.

**L5 — hallucination and faithfulness judge** (`layers/l5_judge.py`). Runs **only when L1–L4 all pass**. This is the layer that catches hallucination *proper*: the citation is real, the output is grounded in it, and it answers the question — but the substance is subtly wrong. [arXiv:2512.15068](https://arxiv.org/html/2512.15068) is the reason this layer exists and the reason it must be a reasoning model: RLHF-aligned hallucinations *"preserve the 'vibe' of the truth while altering the facts"*, are **semantically indistinguishable from faithful responses** to embeddings and NLI models (100% FPR on real hallucinations), and only reasoning-based judges succeeded (GPT-4 at 7% FPR). L3/L4 provably cannot do this; L5 is not a rubber stamp on their work, it is the only layer attempting the task.

Opus via OpenRouter, receiving the source passages L3 retrieved plus the full findings set as structured JSON. Rubric, each 0–4 with written justification:

| Dimension | Question |
|---|---|
| `factual_faithfulness` | Does the output assert anything the source does not support, or misstate what it holds? **Weighted highest** |
| `contextual_accuracy` | Does it use the case for what it actually decides — the brief's "why a case matters" vs keyword matching? |
| `citation_integrity` | Is the proposition genuinely attributable to the cited authority? |
| `responsiveness` | Does it answer the whole question, including multi-part questions L4 can miss? |

Give the judge the **retrieved passages, not the whole judgment** — it makes the faithfulness call checkable against specific text and keeps the prompt cacheable. Request `response_format: json_schema, strict: true` but **never assume enforcement**: parse ladder = strict → fenced block → first balanced object → one repair retry → `JUDGE_UNPARSEABLE` (WARN, doesn't change the verdict). Two impls behind `JudgeProvider`: `openrouter` (default, `anthropic/claude-opus-5`) and `anthropic_direct` (`claude-opus-5`).

---

## Thresholds — what the literature actually says

I went looking for a defensible number. **The literature's clearest finding is that a fixed, transferable cosine threshold does not exist**, and one paper directly undermines using embeddings for this task at all. That changes the role L3/L4 play, so it is worth stating precisely.

### The four relevant results

**1. Absolute cosine values are not interpretable, and thresholds do not transfer.**
*"Semantics at an Angle: When Cosine Similarity Works Until It Doesn't"* ([arXiv:2504.16318](https://arxiv.org/html/2504.16318v3)) — verified by reading — states it flatly: *"A score of 0.8 is not a probability, and a threshold learned for one model, layer, language, or corpus need not transfer to another."* The paper deliberately **provides no universal threshold values**. It names four failure modes, one of which lands squarely on L3: **task–score mismatch — cosine is symmetric and cannot represent asymmetric relations like entailment.** "Is this claim supported by this passage" *is* an entailment relation. Cosine is a proxy for it, not a measure of it.

**2. Cosine similarity can be arbitrary.**
Steck, *"Is Cosine-Similarity of Embeddings Really About Similarity?"* (WWW'24, [arXiv:2403.05440](https://arxiv.org/abs/2403.05440)) — learned embeddings carry degrees of freedom that make cosine similarities non-unique and implicitly controlled by the regularization used in training. The authors caution against using cosine similarity blindly.

**3. Anisotropy breaks absolute values but preserves ranking.**
*"Calibrated Similarity for Reliable Geometric Analysis of Embedding Spaces"* ([arXiv:2601.16907](https://arxiv.org/abs/2601.16907)) — anisotropy systematically miscalibrates absolute cosine values (scores concentrate in a narrow band regardless of actual relatedness) **while rank correlation with human judgment survives**. Isotonic regression on human judgments restores calibration without changing ranking. *Practical consequence: trust relative comparisons, distrust absolute cutoffs.*

**4. The one that matters most — embeddings provably fail on real LLM hallucinations.**
*"The Semantic Illusion: Certified Limits of Embedding-Based Hallucination Detection in RAG Systems"* ([arXiv:2512.15068](https://arxiv.org/html/2512.15068)) — verified by reading. On **synthetic** hallucinations embedding methods reach **95.8% coverage at 0% false-positive rate**. On **real** hallucinations from an RLHF-aligned model (HaluEval/ChatGPT) the same methods yield **100% FPR at target coverage**. RLHF-aligned hallucinations are *"semantically indistinguishable from faithful responses"* — they *"preserve the 'vibe' of the truth while altering the facts."* Even DeBERTa NLI at 0.81 AUC fails, because *"the hardest hallucinations achieve near-perfect entailment scores, forcing thresholds so conservative they flag all faithful responses."* Conclusion: *"semantic similarity is an insufficient proxy for factual faithfulness."* Only reasoning-based judges succeeded (GPT-4 at 7% FPR).

### What this does to the design

It doesn't break the architecture — it tells us which task each layer is allowed to attempt. Once L3 and L4 are framed as **retrieval** questions rather than truth questions, the literature stops being a problem and starts being support: result #3 says **ranking survives** the anisotropy that destroys absolute calibration, and ranking is all retrieval needs.

| Layer | The question it asks | Task type | Why it's the right tool |
|---|---|---|---|
| **L1** | Does this citation exist, and is this quote really in it? | Deterministic lookup | Ground truth. No model, no threshold — 0.26 s and binary |
| **L3** | Does the output *use* this source? | **Retrieval / ranking** | If a RAG system produced this from the document, the content lands in the top-k chunks. Ranking is what survives anisotropy (#3) |
| **L4** | Does the output answer *this* question? | **Retrieval / ranking** | Same shape: question as query, answer chunks as corpus |
| **L5** | Is it actually *true* to the source? | **Reasoning** | Faithfulness is what embeddings provably cannot judge (#4). Only reasoning judges succeeded |

Nothing here asks cosine to decide truth — that was the design error the literature warns against, and it is now confined to L5 where a reasoning model belongs. The pitch line: *we use embeddings for retrieval, where they're proven, and a reasoning judge for faithfulness, because the literature shows embedding similarity cannot detect real LLM hallucinations.*

It also resolves the fail-fast tension: L3/L4 fire only on egregious mismatch, so clean-looking output reaches L5 — which is exactly where the subtle cases need to go.

### Score on the margin, not the absolute value

The single most useful consequence of result #3 is that **a difference of two cosine scores is far more stable than either score alone.** Anisotropy inflates all similarities in roughly the same direction, so it largely cancels in a subtraction — which is why L3 and L4 both score contrastively against a background set rather than against a fixed constant.

| Layer | Signal | FAIL | WARN | PASS |
|---|---|---|---|---|
| L1 quote | `rapidfuzz.partial_ratio`, 0–100 | `< 75` | `75–90` | `≥ 90` |
| **L4 responsiveness** | `max cos(question, output_chunks)` | `< 0.50` | `0.50–0.70` | `≥ 0.70` |
| **L3 grounding** | `max cos(claim, cited) − max cos(claim, BACKGROUND)` | `≤ 0.02` | `0.02–0.08` | `> 0.08` |
| L3 guard | `max cos(claim, cited)` | `< 0.35` regardless of margin | | |

**L4 is a plain absolute threshold, and 0.50 is chosen for where it sits relative to the decision, not because the number means anything on its own** (result #1 says it doesn't). Fail-fast makes a false FAIL unrecoverable, so the rule is **prefer a false green to a false red** — wrongly accusing correct legal work is what destroys trust in an accuracy tool. 0.50 fails only clearly off-topic answers and routes the whole contested band into WARN, which passes and still reaches L5's `responsiveness` dimension.

**L3 keeps a contrastive margin** because its background costs nothing: `BACKGROUND` is ~200 chunks sampled from judgments already in the cache, embedded once. A margin at or below zero is a *meaningful* statement — the cited judgment supports this claim no better than an unrelated judgment does, so the output is not actually using it. That is worth more than a bare score, and it is free.

Bands stay **three-way, not cutoffs**: the ambiguous middle goes to L5 rather than being guessed at. That is the design answer to "no transferable threshold exists."

### Calibrate before demo — ~20 minutes

Both remain seeds. Derive them from the model's own behaviour using the μ ± σ approach from the RAG threshold literature:

1. Assemble ~20 pairs per layer: 10 genuine, 10 **hard negatives** — for L4, same area of law but answering a different question; for L3, a real judgment that doesn't support the claim. Easy off-topic negatives flatter any threshold and teach you nothing.
2. Embed with the real model and correct `input_type` values.
3. Compute μ and σ over the **positives**. Set `FAIL = μ − 2σ`, `PASS = μ − 0.5σ`.
4. Report the false-fail rate on positives (target: zero) and recall on hard negatives — expect it to be modest. That is the honest result and exactly what #4 predicts; L5 is the safety net for the rest.

Thresholds live in `settings.py` **keyed by model**; switching to `voyage-context-4` invalidates all of them.

> **Two accuracy notes on my own evidence.** The F8 figures (0.870 / 0.869 / 0.483 / 0.267) came from a coarse `difflib` sliding window, **not** `rapidfuzz.partial_ratio` — what transfers is the *separation between regimes*, not the absolute values, and `partial_ratio` needs its own calibration. And a frequently-quoted "0.7 retrieval threshold" result I found in search summaries could **not** be verified in the source PDF, so it is deliberately not cited above.

---

## Fetching: HTTP fast path + headless browser for login walls

```
providers/fetcher_http.py     httpx — open sources (eLitigation, AGC SSO). ~0.26 s
providers/fetcher_browser.py  Playwright chromium — login-walled sources (LawNet)
sources/registry.py           domain → SourceAdapter + fetch strategy
```

Both satisfy one `Fetcher` protocol, so layers never know which was used.

- **Persistent authenticated profile.** Playwright launched with a `user_data_dir` on a Docker volume, so one login is reused across runs and restarts. `make login` opens a **headed** browser for the one-time manual sign-in (handles SSO/2FA without us ever touching credentials — we store a browser profile, not a password).
- **Session health check** before each use. Expired or logged out → `SOURCE_UNAUTHENTICATED` → the citation is `UNVERIFIED` (**WARN**), never FAIL. We cannot verify it; that is not evidence of fabrication.
- **Isolated worker.** Browsers are heavy and slow: `ROLE=browserworker`, its own `browser` queue, concurrency ≤2, longer timeouts. It must not sit in the path of the 0.6 s fabrication check.
- **Cache hard.** A login-walled fetch is the most expensive operation in the system; the `documents` cache means it happens once per case, ever.
- Image gets `playwright install --with-deps chromium`; note this materially increases image size and build time — it is the main cost of this capability.

---

## Implementation

### Repo layout

```
smu-lit-2026/
├── Makefile  docker-compose.yml  pyproject.toml  .env.example  README.md
├── docker/Dockerfile  docker/entrypoint.sh    # ROLE=api|worker|judgeworker|browserworker|migrate
├── docs/  01-architecture.md  02-contracts.md  03-findings.md   # 03 = the F1–F11 evidence
├── alembic/versions/0001_initial.py           # ENTIRE schema, one migration
├── src/verifier/
│   ├── settings.py errors.py logging.py
│   ├── contracts/    enums.py citations.py documents.py findings.py
│   │                 layers.py runs.py api.py     # ← FROZEN, pure pydantic, no internal imports
│   ├── providers/    base.py factory.py fetcher_http.py fetcher_browser.py
│   │                 voyage.py anthropic_llm.py openrouter_llm.py
│   │                 mock/{llm,embeddings,fetcher}.py
│   ├── sources/      base.py registry.py  elitigation/{citation_url,client,search,parser}.py
│   │                 lawnet/{client,parser}.py
│   ├── extraction/   patterns.py citations.py quotes.py sources.py attribution.py
│   ├── layers/       base.py registry.py l1_existence.py l2_lists.py
│   │                 l3_alignment.py l4_responsiveness.py l5_judge.py prompts/*.md
│   ├── semantic/     chunking.py contextualise.py embed.py similarity.py
│   ├── pipeline/     orchestrator.py gate.py aggregate.py events.py
│   ├── repos/        base.py models.py session.py documents.py resolutions.py
│   │                 embeddings.py lists.py runs.py memory.py
│   ├── api/          app.py deps.py sse.py routes/{verify,runs,lists,health}.py
│   └── worker/       celery_app.py tasks.py
├── tests/  conftest.py  corpus/*.html  contracts/test_schema_snapshot.py
│           extraction/ sources/ layers/ semantic/ pipeline/ api/
│           pipeline/test_judge_cannot_launder.py   # ← the invariant proof
└── extension/  manifest.json  src/{background,content,selectors,state,api,panel}.js
```

### Schema (`0001_initial.py`)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

documents(id, source_url UNIQUE, source_domain, fetch_strategy,   -- http | browser
          fetched_at, http_status, is_soft_404,
          neutral_citation, case_name, court, year, decision_date, coram,
          text, text_sha256, char_len,
          parallel_citations text[],   -- from <nobr> (F5): lets report cites resolve later
          cited_authorities text[])    -- free citation graph; hook for a future bias layer
document_paragraphs(id, document_id, ordinal, para_no, kind, heading_path text[], text)
citation_resolutions(id, citation_key UNIQUE, citation_type, status, method,
                     document_id, candidates jsonb, confidence, resolved_at, expires_at)
text_embeddings(id, model, input_sha256, dim, embedding vector(1024),
                UNIQUE(model, input_sha256))
document_summaries(id, document_id, model, prompt_version, summary,
                   UNIQUE(document_id, model, prompt_version))
chunks(id, document_id, ordinal, kind, text, text_sha256, embed_input_sha256, para_from, para_to)
list_entries(id, list_type, match_type, pattern, reason, active)   -- domain|url_pattern|publisher
runs(id, created_at, question, ai_output, ai_output_sha256, idempotency_key UNIQUE,
     status, verdict, verdict_stage, short_circuited, short_circuit_reason,
     deterministic_ms, judge_ms, total_ms, cost_usd, cache_hits, cache_misses, seq)
run_citations(run_id, ordinal, raw_text, citation_type, normalized_key, span, resolution_id)
run_sources(run_id, ordinal, raw_ref, domain, list_type, list_entry_id)
run_quotes(run_id, ordinal, quote_text, attributed_citation_ordinal, attribution_method, pinpoint_para)
layer_results(run_id, layer, status, score, duration_ms, cache_hits, detail jsonb, PK(run_id,layer))
findings(id, run_id, layer, code, severity, status, message, citation_ordinal,
         output_span, evidence jsonb, source)     -- source: deterministic | llm
judge_calls(id, run_id, provider, model, prompt_version, request jsonb,
            response_raw, parsed jsonb, parse_path, retries, latency_ms, cost_usd)
                                       -- full provenance: the auditor's own audit trail
```

**This is the scalability story, and it is measurable.** Every expensive artefact is content-hash keyed, so the second query touching *Spandeck* pays nothing: `citation_resolutions` skips the fetch (including an expensive browser fetch), `document_summaries` skips a Sonnet call, `text_embeddings` skips ~50 Voyage calls. Cold ≈ 1 fetch + 1 summary + ~50 embeddings + 1 Opus call; warm ≈ ~4 embeddings + 1 Opus call; a deterministic failure ≈ **zero LLM tokens**. Singapore appellate law has a heavy head, so hit rates should exceed 90% after warm-up. Return `cache_hits/cache_misses` per run and show the hit rate in the panel — measured, not asserted. Comparisons are scoped to one document's chunks (small N), so ANN indexing is optional; add HNSW only if profiling asks.

### API

| Endpoint | Purpose |
|---|---|
| `POST /v1/verify` | `{question, ai_output, options?, idempotency_key?}` → 202 `{run_id, seq, status}` in ~5 ms |
| `GET /v1/runs/{id}?since_seq=N` | Full state or delta — **the extension's transport** |
| `GET /v1/runs/{id}/stream` | SSE with `Last-Event-ID` replay — for the dashboard |
| `GET/POST/DELETE /v1/lists` | Source black/gray/white CRUD |
| `GET /healthz`, `GET /readyz` | Liveness; readiness reports provider mode + browser session health |

One `RunState` schema serves the 202 body, the poll response, and every SSE payload — one schema, three transports, one renderer. Events: `accepted → extracted → layer_result(L4) → layer_result(L1) → layer_result(L3) → layer_result(L2) → deterministic_verdict → {judge_skipped | layer_result(L5)} → final`. L4 typically lands first (no dependencies), L2 last (needs L1's resolution). Layers publish to Redis pub/sub **and** `RPUSH` to a replay log (TTL 1h), so nothing is lost between `POST` and the client attaching.

### Celery

```
queues: default (orchestrator) · judge (isolated) · browser (isolated) · maintenance
  run_verification(run_id)    → default, soft=45s hard=60s
      L0 → L2a inline → gather(L1, L3, L4) → L2b → publish deterministic → gate → judge | final
      (L1 and L3 share one single-flight resolve_document() per citation — one fetch, two consumers)
  fetch_document(url)         → browser, soft=60s hard=90s
  judge_verification(run_id)  → judge,   soft=90s hard=120s
  prewarm_document(key)       → maintenance
```
`ROLE=worker` runs `default,maintenance` (concurrency 8); `ROLE=judgeworker` runs `judge` (concurrency 4); `ROLE=browserworker` runs `browser` (concurrency 2). Isolation is the point: a 15 s Opus call and a 5 s browser fetch must never block the 0.6 s fabrication check. `task_acks_late=True`, `worker_prefetch_multiplier=1`; `run_verification` is a no-op if the run is already complete.

### Dependencies

Floors, not pins — `uv lock` produces the exact set at build time:

`fastapi>=0.115` · `uvicorn[standard]>=0.32` · `sse-starlette>=2.1` · `pydantic>=2.9` · `pydantic-settings>=2.6` · `celery[redis]>=5.4` · `redis>=5.2` · `sqlalchemy>=2.0` · `alembic>=1.13` · `psycopg[binary,pool]>=3.2` · `pgvector>=0.3` · `httpx>=0.28` · **`playwright>=1.48`** · `selectolax>=0.3` (≈20× faster than BeautifulSoup on 150 KB pages, and we parse thousands) · `rapidfuzz>=3.10` · `voyageai>=0.3` · `anthropic>=0.49` · `tenacity` · `structlog` · `orjson` · `python-ulid` · `pyyaml`.
Dev: `pytest>=8.3` · `pytest-asyncio` · `pytest-socket` · `respx` · `ruff>=0.8` · `mypy`.

OpenRouter is called with raw `httpx` — pulling in the OpenAI SDK for one endpoint isn't worth it. Ruff line-length 100, `E/F/I/UP/B`, `from __future__ import annotations`, Python 3.12 (`.python-version`; local is 3.14, so `uv python install 3.12` is part of `make setup`).

### Compose

`postgres` (`pgvector/pgvector:pg17`) · `redis` · `migrate` · `api` · `worker` · `judgeworker` · `browserworker` (+ `browser-profile` volume). One image, `ROLE` dispatch. Everything boots with an empty `.env` at `PROVIDER_MODE=mock`.

**Docker Desktop is not running on this machine.** `make up` must fail with a message saying so, and `make dev` is the primary path: only `postgres` + `redis` in Docker, api and worker native via uv, so hot-reload works and a container build is never on the critical path to a demo.

### Mock mode

`PROVIDER_MODE=mock` boots and passes tests with **zero keys and no network** (`pytest-socket` enforces it).
- Mock fetcher serves `tests/corpus/*.html` — real saved judgments plus the captured soft-404 — for **both** strategies, so the browser path is testable offline too. **L1 and L2 are therefore fully real in mock mode**; only L3/L4/L5 are stubbed. The layers that produce failures are the ones testable offline, which is the right split.
- Mock embedder is a **hashed bag-of-words** vectorizer (token → bucket, L2-normalized), *not* random vectors, so threshold logic is genuinely exercised.
- The verified Spandeck page is already captured at `…/scratchpad/spandeck.html`; seed the corpus from it.

### Chrome extension

MV3. `host_permissions: ["http://localhost:8000/*"]`, content script on `https://claude.ai/*`, `permissions: [storage, contextMenus, activeTab]`, no `<all_urls>`. API CORS allows `chrome-extension://*`.

**Poll, don't stream.** MV3 service workers are evicted after ~30 s idle and lack `EventSource`; a content-script `EventSource` may be blocked by claude.ai's CSP, and that is not a bug to debug on demo day. Poll `GET /v1/runs/{id}?since_seq=N` at 400 ms from the content script — a run is ≤20 s, so ≤50 cheap local requests. SSE stays server-side for the dashboard.

**Assume the selectors break.** Four tiers in `selectors.js`: `data-testid` → ARIA/`role="article"` → structural heuristic (repeated sibling group at max depth; user vs assistant by class-token frequency, no hardcoded class names) → **manual trigger** (toolbar button + context-menu "Verify this response"), always available so a demo never dies on a DOM change.

### The capture contract

The extension produces exactly this, and it is the system's only input:

```jsonc
{
  "question":  "…",   // the user turn immediately preceding the response
  "ai_output": "…",   // the assistant turn, as plain text
  "context":   [ {role, text}, … ],   // up to 3 prior turns, for follow-up disambiguation
  "is_followup": true                 // set when `question` cannot stand alone
}
```

- **Pair correctly.** In a long thread, walk *up* from the assistant node being verified to the nearest preceding user node. Do not assume "last user message" — the user may have typed a new prompt while an earlier response is being verified.
- **Normalize to text.** Strip markdown to readable text but **preserve quote marks and blockquote structure** — L1's quote extraction depends on quote delimiters (correction 3), so a naive `innerText` that flattens `>` blockquotes loses the signal. Keep code blocks and tables as text; drop UI chrome (copy buttons, "Retry", footnote markers).
- **Capture cited URLs.** If Claude used web search, the rendered citation links are real source domains — hand them to L2a directly rather than re-deriving them.

**Follow-up questions are the false-red trap.** A turn like *"why?"* or *"what about the second limb?"* cannot stand alone, and L4 will score it near-zero against a long answer even when the answer is perfect. Detect it — under ~10 tokens, or opening with a pronoun / "what about" / "and" / "why" — set `is_followup`, and **downgrade L4 to WARN instead of FAIL** for that run. Under fail-fast a false red here is unrecoverable, and follow-ups are the single most likely way to hit one during a live demo.

**Streaming-complete detection** needs both conditions or you verify a half-written answer: `MutationObserver` debounced 1200 ms with no mutations, **and** the stop-generation control absent. Hash the text; if it changes, cancel the in-flight run and restart.

**Panel:** verdict header; one row per layer with status pill, duration, cache-hit indicator; findings with the offending span highlighted via `CSS.highlights` (no DOM mutation of claude.ai's React tree); each citation links to its resolved URL or says "could not be resolved"; source-trust badges from L2; and a visually distinct **"LLM judge (advisory)"** section that is visibly *absent* when the judge was skipped — showing "failed deterministic checks, judge not consulted" is the invariant made legible.

---

## Delivery: staged commits in one PR

The directory is empty and **not yet a git repo**; `gh` is authenticated as `Raphael2908` (`repo` scope), git identity `Raphael2908 <bleghcop@gmail.com>`.

**Setup.** `git init -b main` → initial commit on `main` with only `README.md` + `.gitignore` (a minimal base for the PR to diff against) → `gh repo create smu-lit-2026 --private --source=. --push` → `git switch -c feat/sal-verifier` → the 15 commits below → push → `gh pr create --base main`. One PR into private repo `Raphael2908/smu-lit-2026`.

**The rollback guarantee:** commits are ordered by dependency, each touches a disjoint area, and **`make test` is green at every single commit**. Reverting any one leaves the tree building. Later commits depend on earlier ones, so revert from the top down.

| # | Commit | Contents |
|---|---|---|
| 1 | `chore: scaffold repo, tooling, and Docker skeleton` | `pyproject.toml`, `Makefile`, compose, Dockerfile, entrypoint, `.env.example`, `.python-version`, README |
| 2 | `feat(contracts): freeze schemas and provider protocols` | `contracts/**`, `settings.py`, all `*/base.py`, pre-written `factory.py` + `registry.py`, `conftest.py`, schema-snapshot test |
| 3 | `feat(db): initial schema and repositories` | `0001_initial.py`, `repos/**`, in-memory repo |
| 4 | `feat(sources): eLitigation resolver, search, and parser` | `sources/elitigation/**`, `fetcher_http.py`, `tests/corpus/**` |
| 5 | `feat(sources): headless browser fetcher for login-walled sources` | `fetcher_browser.py`, `sources/lawnet/**`, `make login`, profile volume, `browserworker` role |
| 6 | `feat(extraction): citation, quote, source, and attribution parsing` | `extraction/**` + table-driven regex tests |
| 7 | `feat(layers): L1 citation existence and quote verification` | `l1_existence.py`; soft-404 + `partial_ratio` + the F8 regression test |
| 8 | `feat(layers): L2 source trust lists` | `l2_lists.py`, `repos/lists.py`, seed list; blacklist-fails-before-fetch test |
| 9 | `feat(semantic): chunking, contextualisation, embedding cache` | `semantic/**`, `providers/voyage.py`, mock embedder |
| 10 | `feat(layers): L3 alignment and L4 responsiveness` | `l3_alignment.py`, `l4_responsiveness.py` |
| 11 | `feat(layers): L5 judge via OpenRouter and Anthropic` | `l5_judge.py`, `prompts/**`, both judge providers, parse ladder |
| 12 | `feat(pipeline): orchestrator, fail-fast judge gate, aggregation` | `pipeline/**` + **`test_judge_cannot_launder.py`** |
| 13 | `feat(api): FastAPI routes, SSE, and Celery workers` | `api/**`, `worker/**` |
| 14 | `feat(extension): Chrome MV3 verification overlay` | `extension/**` |
| 15 | `docs: architecture, contracts, and probe findings` | `docs/01-03`, incl. the F1–F11 evidence table |

Commits 4–6, 7–8, and 9–11 are the parallel streams: *authored* concurrently, *committed* in dependency order so the history stays linear and bisectable. **Commit 12 is the integration point and the most important commit in the PR** — it carries the fail-fast gate and the invariant that makes the whole thing defensible.

---

## Verification

```bash
make setup       # uv python install 3.12 && uv sync && playwright install chromium
make lint        # ruff + mypy
make test        # sockets blocked, no keys, mock providers — MUST pass with an empty environment
make dev         # postgres+redis in Docker, api+worker native (start Docker Desktop first)
make login       # headed browser, one-time manual sign-in to LawNet; persists the profile
make smoke       # POST a sample verify with an empty .env, assert 200 + a verdict
make seed-lists  # curated source black/gray/white entries
```

Targeted assertions drawn from the verified evidence:
- `[2007] SGCA 37` resolves; `[2019] SGCA 999` is caught as a soft-404 → FAIL `CITATION_NOT_FOUND`.
- A one-word-drifted quote **passes** (F8: fuzzy 0.869) while a fabricated holding fails — the regression test against reintroducing Ctrl+F.
- `[2007] 4 SLR(R) 100` alone yields `UNVERIFIED`/WARN, never FAIL (F7).
- A blacklisted source fails in ~5 ms with **no fetch and no worker task dispatched**.
- An expired browser session yields `UNVERIFIED`/WARN, never FAIL.
- **Complete report on a fabricated citation**: the run FAILS at L1, and L4 still reports a real score while L3 reports `NOT_APPLICABLE` — so the panel can say "citation fabricated, but the answer does address your question." Assert L4's result is present, not skipped.
- **Capture contract**: a fixture of saved claude.ai DOM pairs the right user turn to the right assistant turn in a 6-turn thread, and preserves blockquote quote marks through normalization (L1 depends on them).
- **Follow-up handling**: a `"why?"` turn sets `is_followup` and yields L4 = WARN, never FAIL.
- **One fetch, two consumers**: assert `resolve_document()` is called once per citation when both L1 and L3 need it.
- **L2 ordering**: a citation resolving to a blacklisted domain is caught by L2b *after* L1 resolves it, not missed for lack of a domain.
- **L3 margin**: a claim paired with its genuine source scores a clearly positive margin; the same claim paired with an unrelated judgment scores ≈0 → FAIL `CLAIM_NOT_GROUNDED_IN_SOURCE`.
- **L4**: an on-point answer clears 0.70; an answer to a different question falls below 0.50.
- `BACKGROUND` is asserted non-empty, ≥100 chunks, spanning ≥5 areas of law — L3's margin is meaningless otherwise.
- **`test_judge_cannot_launder`**: on a fabricated-citation fixture the judge provider is asserted **never called**, and the verdict is FAIL.

Live path (`VOYAGE_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` — **none currently set**): verify a real Claude answer about Spandeck end-to-end, then run it twice and report the cache-warm latency delta as the scalability number.

**Demo:** load the unpacked extension → ask claude.ai a Singapore case-law question → badge amber → green with the per-layer breakdown → then paste a deliberately fabricated citation → **red in under a second**: *"[2019] SGCA 214 does not exist"*, with "judge not consulted — failed deterministic checks" and no Opus spend.

**The beat that lands with a lawyer** is the panel underneath that red: L4 still shows a passing responsiveness score. *"The citation is fabricated — but the answer does address your question."* That is the difference between a tool that says "no" and one a practitioner would actually use. Finish by pasting an answer sourced from a blacklisted site and watching it fail in milliseconds.

---

## Risks

1. **Embedding similarity cannot detect real LLM hallucinations — design around it, don't hide it.** [arXiv:2512.15068](https://arxiv.org/html/2512.15068) measured 95.8% coverage at 0% FPR on synthetic hallucinations but **100% FPR on real RLHF-model hallucinations**, which *"preserve the 'vibe' of the truth while altering the facts."* Do not pitch L3/L4 as the hallucination defence — L1 is, and it's deterministic. Say this proactively: it explains why the architecture is shaped the way it is, and a judge who knows this literature will otherwise ask.
2. **Thresholds gate the verdict directly (see Thresholds above).** The starting values — L4 FAIL `< 0.50`, L3 FAIL `< 0.45`, L1 quote FAIL `< 75` — are reasoned seeds, not measurements, and [arXiv:2504.16318](https://arxiv.org/html/2504.16318v3) is explicit that no threshold transfers across models. Run the 20-pair μ ± σ calibration before demo day, with hard negatives. Shipping uncalibrated thresholds in a tool that judges other tools' rigour is the most embarrassing failure available.
3. **Keep L3 on the retrieval question, not the entailment question.** Cosine cannot represent asymmetric relations like entailment ([arXiv:2504.16318](https://arxiv.org/html/2504.16318v3), failure mode 4), so L3 must stay "does the output use this source?" and never drift into "does this claim follow from it?" — that is L5's. The temptation to tighten L3 until it catches wrong holdings is the trap; the upgrade path if L3 underperforms is a cross-encoder or NLI reranker over the top-k passages, not a higher threshold.
4. **`BACKGROUND` is part of L3's contract.** Its margin is only meaningful relative to that set. If it gets seeded with judgments on the same topic as the query, margins collapse and everything looks ungrounded. Sample broadly across areas of law, version it, and assert its size and diversity in a test.
2. **The browser is the fragile part.** Session expiry, SSO/2FA re-prompts, layout changes, and a much larger image. Mitigations: `UNVERIFIED` (never FAIL) on auth failure, `/readyz` reports session health, isolated queue with concurrency 2, aggressive caching, and `make login` to re-auth in seconds. Test the login-walled path *before* demo day.
3. **Rate-limit every source.** No `robots.txt` is not permission, and a logged-in session is more sensitive still. Global semaphore (≤2 concurrent, ≥250 ms spacing), descriptive User-Agent, aggressive caching, circuit breaker. Respect LawNet's terms — check whether automated access is permitted under the subscription before demoing it.
4. **`Filter=SUPCT` is Supreme Court.** Results did surface `SGDC`/`SGMC`/`SGFC`, so it may be broader than the name suggests, but the State Courts filter value is unverified. Until checked, State Court citations resolve by direct URL (which works) and degrade to `UNVERIFIED` on name-only search.
5. **The judge's own bias is unaudited.** Opus judging (often) Claude output is a real conflict. Say so, with three mitigations: the deterministic layers are model-independent and run first; `JUDGE_PROVIDER` can point at a non-Anthropic model for a cross-model check; and `judge_calls` retains full provenance, so every judge verdict is itself auditable. Volunteering this beats hoping nobody asks.
6. **claude.ai's DOM will break.** Four selector tiers plus the always-available manual trigger. Budget for it.
7. **Docker daemon down, no keys, Python 3.14 locally.** All three must produce clear error messages, and `make dev` must work without a full container build.


---

## What changed during the build

This plan is preserved as approved. Five things moved once code met reality; the
architecture docs carry the current truth.

1. **A third page state appeared (F12).** The plan knew about real judgments and
   soft-404s. A live maintenance window revealed an 819-byte notice page that the
   planned `len < 10_000` rule would have classified as a *fabricated citation* —
   reporting every real Singapore case as hallucinated for the duration of any outage.
   The `<title>` is the discriminator; length is only a corroborator. This generalised
   into a rule the whole system now follows: **"cannot verify" is never "fabricated."**

2. **L1 quote thresholds were recalibrated under the real metric.** The plan's figures
   came from a coarse `difflib` probe. Measured under `rapidfuzz.partial_ratio`:
   verbatim 100.0, one word changed 94.5, paraphrase 49.7, fabrication 46.1. The
   plan claimed paraphrase scores *below* fabrication; it does not — they are
   indistinguishable, 3.6 points apart. The design conclusion is unchanged and better
   supported: lexical matching cannot separate an honest restatement from fiction, so
   L1 scores only text presented as a direct quotation.

3. **Thresholds became model-keyed in code, not just in principle.** The plan said
   thresholds do not transfer between models. The hashed bag-of-words mock *is* a
   different model, and applying the real-model 0.50/0.70 to it fails every answer —
   painting a fully green run red on L4 alone. `settings.py` now resolves L4
   thresholds by model, measured rather than guessed.

4. **`LayerInput` gained `documents`, and `EmbeddingRepo.put_many` gained
   `document_id`.** Two contract gaps the implementations found: L1 had no route to
   the judgment text it needs, and without document attribution a cached claim could
   enter a later run's background pool and be compared against itself.

5. **CORS needs `https://claude.ai`, not only `chrome-extension://*`.** A content
   script's `fetch` carries the *page* origin. The extension-only policy the plan
   implied would have failed every request in preflight, with nothing obviously wrong.

Deliberately unbuilt, as scoped: the bias layer and the labelled benchmark harness.
The architectural rule that answers "who audits the auditor" ships as an executable
test instead — `tests/pipeline/test_judge_cannot_launder.py`.
