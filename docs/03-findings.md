# Evidence base

Everything here was established by live probing or by reading the cited paper. Nothing
in this file is inferred. Numbers that were *not* verified are marked as such — that
distinction matters more than usual in a project whose pitch is rigour.

## Part 1 — the Singapore corpus (eLitigation)

| # | Finding | Evidence |
|---|---|---|
| F1 | Neutral citation maps to a deterministic URL | `[2007] SGCA 37` → `/gd/s/2007_SGCA_37`; confirmed for SGCA and SGHC |
| F2 | Judgments are static HTML — no JS, no browser needed | 115,404 chars of full text in **0.26 s** over plain HTTP |
| F3 | **A fabricated citation returns HTTP 200**, not 404 | Soft-404 body is ~3.5 kB against ~150 kB for a real judgment |
| F4 | The right-case check is free | Real pages carry `<title>[2007] SGCA 37</title>`; party names appear verbatim in the text |
| F5 | Judgment markup is cleanly structured | `Judg-1` ×150 (numbered paragraphs), `Judg-Quote-1` ×49, `Judg-Heading-1/2/3` ×8/17/22, `<nobr>` around parallel citations |
| F6 | Case-name search works, and is binary | `Tan Cheng Bock v AG` → `2017_SGCA_50` at rank 1; two fabricated case names returned **0 hits** each |
| F7 | **Report citations do not resolve** | `"[2007] 4 SLR(R) 100"` returned 10 hits, none of them the case. The index is full-text, so it finds cases that *cite* the report |
| F9 | A judgment does not fit one embedding call | Body 83,808 chars ≈ **21K tokens** vs voyage-law-2's 16K context — chunking is mandatory |
| F10 | `SGHC(A)` becomes `SGHCA` in the URL | Parenthesised court suffixes are stripped, not encoded |
| **F12** | **There is a THIRD page state: site maintenance** | Discovered when eLitigation went down mid-build. See below — this one is dangerous |

### F12 — the maintenance page, and why it nearly caused a catastrophic false positive

A fetch can land in three states, and body length cannot separate two of them:

| State | Bytes | `<title>` |
|---|---|---|
| Real judgment | 150,389 | `[2007] SGCA 37` |
| Fabricated citation (soft-404) | 3,549 | `''` (empty) |
| **Site maintenance** | **819** | `:: eLitigation - Maintenance Notice ::` |

A naive `len(html) < 10_000 → not found` rule classifies the maintenance page as a
**fabricated citation** — meaning that during any maintenance window the system would
report every real Singapore case as hallucinated. That is the worst failure this
product can have, and it would have looked like a working demo right up until it
didn't.

**The `<title>` is the discriminator; length is only a corroborator.**

```
title == requested neutral citation      -> RESOLVED
title empty AND "Page Not Found" in body -> NOT_FOUND        (fabricated; the only FAIL)
title non-empty but not a citation       -> SOURCE_UNAVAILABLE (WARN, never FAIL)
```

The general rule this instantiates: **"cannot verify" is never "fabricated."** It
applies equally to report-only citations (F7), an expired login-walled session, and a
source outage. Only positive evidence of non-existence may fail a run.

## Part 2 — what the literature says about cosine thresholds

Searched because a threshold number was needed. The clearest finding is that a fixed,
transferable one **does not exist**.

1. **Thresholds do not transfer.** *"Semantics at an Angle: When Cosine Similarity
   Works Until It Doesn't"* ([arXiv:2504.16318](https://arxiv.org/html/2504.16318v3))
   — read directly. Verbatim: *"A score of 0.8 is not a probability, and a threshold
   learned for one model, layer, language, or corpus need not transfer to another."*
   It deliberately supplies no universal values. It also names four failure modes, one
   of which binds L3: **cosine is symmetric and cannot represent asymmetric relations
   like entailment.**

2. **Cosine can be arbitrary.** Steck, *"Is Cosine-Similarity of Embeddings Really
   About Similarity?"* (WWW'24, [arXiv:2403.05440](https://arxiv.org/abs/2403.05440))
   — learned embeddings carry degrees of freedom that make cosine non-unique and
   implicitly governed by training regularization.

3. **Anisotropy breaks absolute values but preserves ranking.** *"Calibrated
   Similarity for Reliable Geometric Analysis of Embedding Spaces"*
   ([arXiv:2601.16907](https://arxiv.org/abs/2601.16907)) — scores concentrate in a
   narrow band regardless of true relatedness, while rank correlation with human
   judgment survives. **Trust relative comparisons; distrust absolute cutoffs.**

4. **Embeddings provably fail on real LLM hallucinations.** *"The Semantic Illusion"*
   ([arXiv:2512.15068](https://arxiv.org/html/2512.15068)) — read directly. Embedding
   methods reach **95.8% coverage at 0% FPR on synthetic** hallucinations but **100%
   FPR on real** ones from an RLHF-aligned model. Those hallucinations are
   *"semantically indistinguishable from faithful responses"* and *"preserve the 'vibe'
   of the truth while altering the facts."* Even DeBERTa NLI at 0.81 AUC fails, because
   *"the hardest hallucinations achieve near-perfect entailment scores, forcing
   thresholds so conservative they flag all faithful responses."* Only reasoning-based
   judges succeeded (GPT-4 at 7% FPR).

### What this does to the design

Each layer attempts only the task its tool is proven for:

| Layer | Question | Task type | Why this tool |
|---|---|---|---|
| L1 | Does this citation exist? | Deterministic lookup | Ground truth. No model, no threshold |
| L3 | Does the output *use* this source? | **Retrieval / ranking** | Ranking is what survives anisotropy (#3) |
| L4 | Does the output answer *this* question? | **Retrieval / ranking** | Same shape |
| L5 | Is it *true* to the source? | **Reasoning** | Faithfulness is what embeddings cannot judge (#4) |

Nothing asks cosine to decide truth — that was the design error the literature warns
against, and it is confined to L5 where a reasoning model belongs.

### Thresholds — now measured against voyage-law-2, not estimated

| Layer | Signal | FAIL | WARN | PASS |
|---|---|---|---|---|
| L1 quote | `rapidfuzz.partial_ratio` 0–100 | `< 75` | `75–90` | `≥ 90` |
| **L4** | `max cos(question, output_chunks)` | **`< 0.40`** | `0.40–0.45` | `≥ 0.45` |
| L3 | `max cos(claim, cited) − max cos(claim, BACKGROUND)` | `≤ 0.02` | `0.02–0.08` | `> 0.08` |
| L3 floor | `max cos(claim, cited)` | `< 0.35` regardless of margin | | |

#### L4, measured (Part 4 below): the 0.50 seed was failing correct answers

The original `0.50 / 0.70` figures were reasoned, not measured. Run against real
`voyage-law-2` they **failed three of five correct answers**. See Part 4.

L3 scores on a **margin** because a difference of two cosines is far more stable than
either alone — anisotropy inflates both in the same direction and largely cancels.
"The cited judgment supports this claim no better than an unrelated judgment does" is
a meaningful statement; "0.55" is not.

**These are reasoned seeds, not measurements.** Calibrate per model with ~20 pairs
(10 genuine, 10 *hard* negatives — same area of law; easy off-topic negatives flatter
any threshold), then set `FAIL = μ − 2σ` and `PASS = μ − 0.5σ` over the positives.
Thresholds are keyed by model in `settings.py`; changing `EMBEDDINGS_MODEL` invalidates
all of them.

Governing rule everywhere: **prefer a false green to a false red.** Fail-fast makes a
false FAIL unrecoverable, and wrongly accusing correct legal work is what destroys
trust in an accuracy tool.

## Part 3 — L1 quote matching, measured under `rapidfuzz.partial_ratio`

The design-phase figures came from a coarse `difflib` sliding window. These are the
real ones, measured against Spandeck paragraph [115] through L1's actual normalisation
pipeline (NFKD, curly→straight quotes, dash folding, whitespace collapse, casefold):

| Regime | `partial_ratio` | Exact substring |
|---|---|---|
| Verbatim | **100.0** | ✅ |
| Curly quotes + en dash + NBSP | 98.0 | ❌ |
| One word changed | **94.5** → PASS | ❌ |
| Several words rewritten | 85.2 → WARN | ❌ |
| Paraphrase (honest restatement) | **49.7** | ❌ |
| Fabrication (invented sentence) | **46.1** | ❌ |

Two things this settles.

**Exact substring matching is unusable.** It returns False on every row but the first —
including a quote that differs only in typography. A Ctrl+F implementation would accuse
correct legal writing of fabrication as a matter of routine.

**Paraphrase and fabrication are indistinguishable: 49.7 vs 46.1.** A 3.6-point gap is
noise. (Note this is the opposite direction from the earlier `difflib` measurement,
where paraphrase scored *below* fabrication — the direction was never the point, and it
does not reproduce. What reproduces is that the two are not separable.) Lexical
similarity cannot tell an honest restatement from plausible fiction, and both sit far
below the 75 threshold, so scoring paraphrased attribution would fail correct legal
writing on what amounts to a coin flip.

That is why `ExtractedQuote.delimiter` is a required field: **L1 may only ever score
text that was presented as a direct quotation.** Whether the output's paraphrased
claims are supported is L3's question, not L1's.
`tests/layers/test_l1_existence.py::test_paraphrase_and_fabrication_are_indistinguishable`
pins this so it cannot silently regress.

The 75 / 90 seeds sit cleanly between the regimes; no change needed.

### One figure we deliberately do not rely on

A "0.7 retrieval threshold" that appeared in search summaries **could not be verified**
in the source PDF, so it is cited nowhere.


---

## Part 4 — measured against real `voyage-law-2`

Everything above this point was either estimated or measured against the mock
embedder. These are the first numbers from the real model, using the pipeline's own
`input_type` asymmetry (`query` for questions and claims, `document` for chunks).

### L4 — question → answer

| Regime | n | μ | σ | min / max |
|---|---|---|---|---|
| On-point | 5 | 0.528 | 0.119 | min **0.434** |
| Hard negative — *same area of law, different question* | 4 | 0.280 | 0.055 | max **0.369** |
| Off-topic | 2 | 0.196 | — | max 0.235 |

**The 0.50 seed failed 3 of 5 correct answers.** The threshold now sits at **0.40**,
in the gap between on-point minimum (0.434) and hard-negative maximum (0.369): zero
false fails on correct answers, all four hard negatives caught.

Note how close the regimes are. The literature's warning that absolute cosine values
are uninterpretable is not abstract — a plausible-sounding 0.50 sits *inside* the
correct-answer distribution, and under fail-fast that silently rejects good legal
work. This is the single clearest vindication of the "prefer a false green to a false
red" rule.

### L3 — claim → cited document, on **raw** paragraphs

| Regime | cited (min/max) | margin (min/max) |
|---|---|---|
| Genuine — supported by Spandeck (n=3) | min **0.501** | min **+0.205** |
| Foreign — true law, not from Spandeck (n=2) | max **0.251** | max **+0.117** |

Separation is wide on both signals, and the current floor (0.35) and margin (0.02)
fail **0 of 3** genuine claims. Unlike the mock embedder — where the margin was
anti-discriminative — the contrastive margin **does** work with a real model, which is
what it was designed for.

### The open contradiction: the contextual prefix appears to hurt retrieval

Those L3 numbers are from **raw** paragraphs. In the live pipeline every chunk is
embedded with a document summary and heading path prefixed to it, and there a
genuinely grounded claim was **failed** by L3.

The prefix is diluting the signal. The thresholds were deliberately **not** lowered to
compensate: that would be tuning around a bug rather than fixing it, and it would hide
a real regression behind a green demo. `EMBEDDINGS_MODEL=voyage-context-4` exists
precisely to A/B this — Voyage's native contextualised endpoint does the same job
without hand-built prefixes. See `todo.md`.

### Caveat that applies to every number here

`n=11` for L4 and `n=5` for L3. These are **working calibrations, not benchmarks**.
They are enough to replace a demonstrably wrong threshold with a measured one; they
are not enough to quote a confidence interval. Widen the sample before relying on
them, and re-run everything for any other embedding model — no cosine threshold
transfers ([arXiv:2504.16318](https://arxiv.org/html/2504.16318v3)).

---

## Part 5 — measured during the first full end-to-end run

Everything here comes from running the real pipeline (mock fetcher over the real
corpus HTML, real `voyage-law-2`, real `anthropic/claude-sonnet-5` judge via
OpenRouter) against Spandeck, and from measuring the chunker directly. eLitigation was
in a maintenance window throughout, so the live-fetch path is untested here — F12's
third page state, again, and the reason the mock fetcher serves real bytes.

### F13 — the judge was reading a quarter of each passage, chosen by byte offset

`RetrievedPassage.render()` truncated at 1,800 characters. Measured against
`tests/corpus/2007_SGCA_37.html` through the production parser and chunker:

| | |
|---|---|
| Paragraphs / characters | 257 / 112,762 |
| Chunks | 43 |
| Chunk characters — min / median / max | 225 / **2,042** / **7,103** |
| Chunks exceeding the 1,800-char passage budget | **22 of 43** |

So for half the corpus the judge received the opening quarter of a passage, cut at a
byte offset rather than by relevance. Worse, the passage was labelled with
`chunk.paragraph_from` — the *first* paragraph of a merge — so `at [187]` could head
text drawn from [188]–[190], inviting the judge to attribute a proposition to a
paragraph it was never shown.

Compounding it, retrieval was **top-1 per claim**: `best_match` is `top_k(k=1)`, so the
candidate pool was one passage per (citation × attributed claim). `MAX_JUDGE_PASSAGES
= 12` was never the binding constraint, and widening it — the obvious fix — would have
changed nothing.

Fixed: top-k per claim, an over-long chunk split back into its own numbered paragraphs
and ranked, passages labelled with the range they actually cover, and the budget spent
round-robin so every attributed claim is represented before any claim gets depth. On
the live run this took the judge's evidence from 5 passages to 21. **The score is
untouched** — L3 still scores `max cos(claim, chunks)`, so every threshold in Part 4
stands, and `test_widening_retrieval_does_not_move_the_score` pins that.

### F14 — the contextual prefix is confirmed to fail correct legal work

Part 4 left this as "the open contradiction". It is no longer open.

**Live, first run.** A correct answer citing `[2007] SGCA 37` and quoting paragraph
[115] verbatim: L1 scored the quote **1.000**, L4 scored responsiveness **0.751**, and
**L3 failed it** at 0.325 against the 0.35 floor. The failing claim was the quoted
sentence itself. The chunk containing [115] was not among the retrieved passages at
all.

**A/B against real `voyage-law-2`**, same four claims, same 43 chunks, once with the
prefix the pipeline applies today (a 1,370-character ≈ 342-token summary prepended to
every chunk) and once on raw chunk text:

| | Prefixed (as shipped) | Raw |
|---|---|---|
| Mean max cos over 4 grounded claims | 0.503 | **0.621** |
| Claims falling below the 0.35 floor | **1 of 4** | **0 of 4** |
| The quoted paragraph's own chunk, for the failing claim | 0.304, rank **#11** | **0.431**, rank **#4** |

Every claim scores lower with the prefix. The failing one goes 0.506 → 0.346, crossing
the floor. And the chunk holding the quoted paragraph falls from rank #4 to rank #11,
which is why it never reached the judge even under top-3 retrieval — **the prefix
damages L3 and L5 by the same mechanism.**

The prefix is byte-identical across all 43 chunks of a judgment, so it adds a large
shared component to every document vector that has no counterpart in the query vector
(claims are embedded bare). After L2 normalisation that component costs magnitude the
claim-relevant part used to have, and it compresses the differences *between* chunks —
which is precisely ranking, the one thing cosine is supposed to survive anisotropy for.

Still **not** fixed by lowering the threshold. The raw-text figures are the ones Part 4
calibrated, and they fail 0 of 4.

### F15 — the documented `voyage-context-4` A/B does not exist

`VoyageEmbedder.contextualized_embed` and `uses_native_context` have **zero call sites
in `src/`**. `CachedEmbedder._embed` calls `embed()` unconditionally, and
`l3_alignment._embed_source` applies the manual prefix unconditionally. Setting
`EMBEDDINGS_MODEL=voyage-context-4` today would send chunks to the *ordinary* embed
endpoint under a contextual model's name **and still prefix them by hand** — the
opposite of what `providers/voyage.py`'s own docstring claims. The "one-env-var A/B"
promised in `todo.md` and `docs/v1-plan.md` is unimplemented, and no threshold block
exists for that model either.

### The pipeline, end to end

| Path | Result |
|---|---|
| Grounded answer, `[2007] SGCA 37` | **PASS** — L1/L2/L3 (0.405)/L4 (0.755) then L5 `correctness 1, material_completeness 1` from `anthropic/claude-sonnet-5`, 21 passages, $0.098 |
| Fabricated `[2019] SGCA 999` | **FAIL** in ~9 s — `CITATION_NOT_FOUND`, judge never invoked, **$0.00** |

The cache claim holds under measurement: the second run touching Spandeck reported
**43 cache hits and 1 miss**, the miss being its own question.
