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
| **F13a** | **The corpus has a house citation style: the SAL *SLR Style Guide* (2021)** | Read directly. It is the spec for what the extractor must recognise — see below |

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

### F13a — the corpus has a house citation style, and it is a specification

Source: **SAL, *SLR Style Guide* (2021 ed)**, read directly
([SMU research guide copy](https://researchguides.smu.edu.sg/ld.php?content_id=50481427)).
This is the Academy's own style guide — the sponsor's house rules for how a citation in
this corpus is written — so it is not a nicety to conform to, it is the **specification
for what the extractor must recognise**.

The failure direction is the dangerous one. A citation form the extractor does not
recognise is authority the answer gets no credit for, and L1a fails an output that
appears to cite nothing at all. Every gap below was therefore a **false red** waiting
to happen. Six were found and closed:

| Guide | Rule | What we were doing |
|---|---|---|
| 2-1.2.1 | Court designators include `SGCT`, `SGSCT`, `SGYC` | Not recognised — three courts' citations counted as nothing |
| 2-1.1.2 | Parentheses for volume-organised series (`(1992) 175 CLR 1`), brackets for year-organised (`[2010] 1 SLR 1`) | Brackets only — every volume-organised series silently dropped |
| 2-1.1.4 | Tables of official/unofficial reports per jurisdiction | 15 series recognised; now 28 |
| 2-2.1.2 | `Short Title (Cap 322, 2007 Rev Ed)` · `Short Title 1969 (2020 Rev Ed)` · `Short Title 2016 (Act 19 of 2016)` | A parenthesised qualifier in a short title (`Administration of Justice (Protection) Act`) defeated the match outright |
| 1-3.2.2 | Pinpoint abbreviations `s(s)`, `sub-s`, `reg(s)`, `O`, `r(r)`, `Pt(s)`, `cl`, `sch` | Only `s`/`section`/`reg`/`art` |
| **2-1.5** | **Cite in full once, then by short title or `([1] supra)`** | **Every subsequent reference read as an uncited assertion** |

The last row is the important one. The guide's own worked example:

```
1   ... The case of ANJ v ANK [2015] 4 SLR 1043 ("ANJ") stands for the proposition that ...
8   As was discussed in ANJ ([1] supra) ...
20  This point was raised in ANJ at [32].
```

Paragraphs 8 and 20 are *correctly cited*. A verifier counting only full citations reads
them as unsupported assertions — penalising the citation style the sponsor mandates.
That was the single largest source of false positives available to L1a, and it is now
recognised: a short title the output itself defined, and `supra`/`ibid`, both count as
authority.

**One thing the guide confirmed rather than changed.** Para 2-1.1.5: a paragraph
pinpoint "should be in brackets, eg, `[2001] 3 SLR 10 at [16]`", while a page pinpoint
is written bare, "without being preceded by 'p' or 'pp' or 'page(s)'". The extractor had
already refused to read a bare `at 294` as a paragraph number, by reasoning about law
reports. The guide makes that decision authoritative rather than merely sensible.

`tests/extraction/test_slr_style.py` pins all of it against the guide's own examples,
paragraph by paragraph.

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

#### The mechanism, measured: the summary collapses the judgment to a point

The explanation above ("a large shared component") is now a number. Mean pairwise
cosine **between Spandeck's own 43 chunks**, which is exactly the quantity ranking
inside a document depends on:

| Regime | mean pair | min pair | spread |
|---|---|---|---|
| Raw chunk text | 0.426 | 0.059 | **0.574** |
| Heading path only | 0.435 | 0.071 | 0.565 |
| Summary + heading (as shipped) | **0.894** | 0.760 | **0.106** |
| `voyage-context-4`, raw text | **0.940** | 0.857 | **0.060** |

Raw, the two least similar chunks of the judgment are nearly orthogonal. Prefixed, no
two chunks are more than 0.24 apart: the case stops being 43 distinguishable passages
and becomes one blurred point. That is why the paragraph an answer quotes verbatim
falls from rank #2 to #16 — there is barely any ranking left to be right about.

#### It is the summary, not the prefix. The heading path is nearly free

Decomposing the two halves over the same 5 claims, background rebuilt inside each arm
so the margin gate is measured and not assumed:

| Arm | mean `s_cited` | mean `s_bg` | mean margin | min margin | FAILs |
|---|---|---|---|---|---|
| Raw | **0.639** | 0.232 | +0.407 | +0.120 | **0/5** |
| Heading only | 0.626 | 0.220 | +0.407 | +0.167 | **0/5** |
| Summary + heading | **0.490** | 0.174 | +0.317 | +0.096 | **1/5** |

The heading path costs 2% of mean similarity, fails nothing, and *improves* the worst
margin. The summary costs 23% and fails correct work. So the fix keeps one and drops
the other — `L3_CONTEXTUAL_PREFIX`, defaulting to `"heading"`.

**The margin gate was never the one firing.** The prefix suppresses `s_cited` and
`s_bg` together, so the contrastive design cancels most of the damage exactly as
intended; every failure in every arm is a floor failure. The earlier suspicion that
L3's background sampling was at fault is not supported: `s_bg` sits at 0.17–0.23
throughout and no claim comes near the margin threshold.

#### Why it looked intermittent

The failing claim, scored across four independent draws of the Haiku summary:

| | heading | draw 1 | draw 2 | draw 3 | draw 4 |
|---|---|---|---|---|---|
| *"the application of a single test was a deliberate departure ..."* | **0.436** | 0.343 | 0.317 | 0.330 | 0.349 |

**All four prefixed draws sit below the 0.35 floor.** The prefix does not fail this
claim by bad luck; it fails it every time, and the summary's own variability (1,190 to
1,617 chars) never rescues it. What *is* intermittent is whether the claim is emitted
at all: `split_claims` is a Haiku call and its output varies between runs, so a given
run may or may not contain the claim that trips. That is why the same answer produced
a red one day and a green the next, and it is a separate pre-existing sensitivity, not
something this fix introduces or removes.

The claim is genuinely grounded — Spandeck discusses the English position at [27] and
[42] and departs from it explicitly — so this was a false red throughout.

#### What changed

`settings.L3_CONTEXTUAL_PREFIX` selects `none` / `heading` / `summary_heading`, with
`heading` the default. Two consequences beyond the score:

- **The summariser is no longer called on the L3 path** unless the regime asks for it,
  which takes a Haiku call off the critical path.
- **The regime namespaces the embedding cache** (`CachedEmbedder.cache_model`).
  Content-addressing already stops a stale vector being *read* after the regime
  changes; it does not stop one being *sampled*, because `sample_background` selects on
  model alone. Without the namespace, flipping this setting would contrast bare chunks
  against prefixed background — a margin between two embedding regimes rather than two
  documents, and a false **green**, so nothing would have gone red to reveal it. The
  cost is a one-time re-embed per judgment (43 calls for Spandeck), and the first run
  under a new regime correctly reports `background_empty`.

### F15 — `voyage-context-4` is the same disease, not the cure

Measured, now that the A/B is runnable. Its absolute scores are not comparable to the
0.35 floor — no threshold transfers between models — but **ranking is** the thing the
literature says survives anisotropy, and on ranking it is the worst arm tested:
within-document chunk compression **0.940** (against raw's 0.426), and the decisive
paragraph falling to rank #12–#38.

That kills the planned "swap the env var and the problem goes away" fix. Making chunks
attend to their neighbours *inside the model* does what stapling a shared summary on
the front does: it pulls every chunk of a case toward the case's centroid. The manual
prefix was never the problem in itself — contextualisation by any means is, because L3
needs to discriminate **within** a document, not between documents.

### F15a — the documented `voyage-context-4` A/B did not exist

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

---

## Part 6 — the fix, re-measured on a live claude.ai answer

Part 5's A/B used claims written for the test. This one replays an answer **Claude
actually produced in the browser**, captured by the extension, through the live
pipeline: real `voyage-law-2`, real `anthropic/claude-sonnet-5`, real Postgres, the
mock fetcher serving the real corpus HTML (eLitigation was in a maintenance window —
F12, a fourth time).

### F16 — the summary costs 24% on live data; the heading path costs ~1%

Identical answer, identical judgment, identical thresholds. The **claim set is pinned**
— see F17 for why that is not optional — so the prefix regime is the only variable:

| Arm | failing claim `s_cited` | worst margin | vs raw |
|---|---|---|---|
| `none` — chunk text only | 0.282 | +0.106 | — |
| `heading` — section path only (**shipped default**) | 0.279 | +0.105 | −1% |
| `summary_heading` — the old behaviour | **0.214** | +0.083 | **−24%** |

The 24% reproduces Part 5's offline figure (23%) on text nobody wrote for the test, and
`none` buys nothing over `heading` — which is the whole basis for keeping the heading
path and dropping only the summary.

It also confirms which gate fires. **Every margin passes** (+0.083 to +0.106 against a
0.02 FAIL threshold, with a real 22-chunk background pool). The floor is the only gate
that has ever failed anything in any measurement in this document.

### F17 — L3 verdicts are not reproducible, because the claim splitter is not

The first attempt at the table above was meaningless and looked fine. Run per-arm
through the orchestrator, the three arms scored **3/14, 4/16 and 4/16** claims — three
different claim sets — and reported `pass`, `fail`, `fail`. The apparent result ("the
shipped default wins") was an artefact of which claims each arm happened to be given.

`chunk_output_claims` calls `Summariser.split_claims`, a Haiku call with no seed and no
cache. The same 2,184-character answer split into 14, 15 and 16 claims across four runs
in one session. Because L3's status is driven by the **worst** attributed claim, a
single extra claim flips the layer, and with it the run's verdict.

Measured consequence, on one answer verified twice through the browser:

| Run | claims | attributed | L3 | verdict |
|---|---|---|---|---|
| First | 14 | 3 | **pass** 0.545 | warn (L1a only) |
| Replay | 15 | 4 | **fail** 0.279 | fail |

Nothing about the answer, the judgment, the thresholds or the code differed. **A green
L3 in this system is not currently a repeatable measurement**, and any A/B run through
the orchestrator without pinning the split is measuring the splitter.

This is a pre-existing defect, not one the prefix change introduced — but it was masked
while the prefix was failing things outright, and it is the reason F14's live failure
looked intermittent. See `todo.md`.

### The claim all three arms fail

> *"The court expressly declined to treat pure economic loss as attracting a separate or
> more restrictive control device, holding instead that the same two stages ... are
> capable of doing the necessary limiting work."*

Spandeck [115] says close to the opposite: *"It could be that a more restricted approach
is preferable for cases of pure economic loss but this is to be done within the confines
of a single test."* The court did not decline a more restrictive approach; it said one
might be preferable, inside the single test.

So the flag may well be a true positive. It should be held loosely: **L3 asks a
retrieval question, not a truth question**, and a claim about what a court *declined* to
do is a negative proposition that will match no single paragraph well even when it is
accurate — the asymmetry arXiv:2504.16318 names and the reason this belongs to L5.
L5 never saw it, because L3 short-circuited the judge.

The open question is whether a floor calibrated on `n=5` positive assertions is the
right instrument for meta-claims of this shape. It is recorded here rather than fixed,
because lowering a floor to admit one claim is exactly the "tuning around the bug"
Part 4 forbids.

### F18 — the claim splitter cuts propositions the answer never made

F17 measured the splitter's *variability*: 14, 15 and 16 claims for one answer, and a
verdict that follows whichever set arrives. This is the other half — what the splitter
does to a claim when it does split it.

It cut one sentence in two and L3 scored the halves separately:

| unit | `grouped` | `paragraph` |
|---|---|---|
| fragment: *"Policy considerations are applied only at the second stage"* | **0.313** | 0.390 |
| fragment: *"… once a prima facie duty of care has been established"* | 0.637 | 0.690 |
| **the sentence the answer actually contains** | **0.649** | **0.700** |

The whole sentence clears the 0.35 floor in **every** configuration, including the
shipped `summary_heading` one at 0.449. Paragraph **[83]** states the claim almost
verbatim — *"Assuming a positive answer to the preliminary question of factual
foreseeability and the first stage of the legal proximity test, a prima facie duty of
care arises. Policy considerations should then be applied…"* — and was ranked **#1
throughout**. Retrieval was never wrong.

The fragment does not say the second stage *of what*. L3 was asked to ground a
proposition with its referent cut away, which is the same defect L1a exists to catch
from the other side.

**Fixed** by a sharpened prompt plus a deterministic guard, because a prompt is a
request: a claim under `L3_CLAIM_MIN_CHARS` is restored to the sentence it was located
in, and two fragments of one sentence collapse to one claim. After the fix the answer
passes in every configuration, **including the original prefixed-and-grouped one** —
the control showing the prefix and the chunking were neither necessary nor sufficient
for the failure that was attributed to them.

`make l3 ARGS="--scenario f14"` re-runs it.

### F19 — a genuine-only claim set cannot tell an improvement from an inflation

Every L3 A/B so far scored claims taken from an answer written to be correct. That
measures the **false-FAIL rate** and nothing else: a change that raises every
similarity passes it while buying no discrimination, because the claims that should
score low rose with the rest. Part 4 did this correctly and the later runs dropped it.

`scripts/l3_probe.py` now carries a fixed calibration set — three genuine claims, each
verified present in a named Spandeck paragraph, and four foreign ones (true Singapore
law this judgment does not decide, from other areas because an easy off-topic negative
flatters any configuration). The reported number is the **GAP**: genuine min − foreign
max.

| arm | mean | below floor | **GAP** |
|---|---|---|---|
| `none` / `grouped` | 0.557 | 1 of 10 | **+0.386** |
| `none` / `paragraph` | 0.612 | 0 of 10 | **+0.380** |
| `summary_heading` / `grouped` | 0.406 | 4 of 10 | +0.248 |
| `summary_heading` / `paragraph` | 0.397 | 4 of 10 | +0.237 |

Dropping the summary is worth **+0.138 of gap** — discrimination, not inflation, and a
stronger justification than the mean scores F14 quoted.

**Paragraph granularity is worth none of it** (+0.386 → +0.380). Finer chunks match
unrelated material better too: the foreign maximum rises 0.254 → 0.320. It is kept for
the passage L5 reasons over — the unit for [83] becomes [83-83] rather than [83-86],
provenance becomes exact, and the quoted paragraph [115] moves from rank #3 to #1 — and
because it is the regime Part 4's thresholds were derived in. Not for its scores.

**Ranking those four arms by mean, or by floor failures, gets the second row wrong.**
That is the finding.


---

## Part 7 — Singapore Statutes Online, measured

eLitigation went into a maintenance window (F12, a fifth time) and the demo answer was
pointed at legislation instead. Everything below was measured on 2026-09-05 through the
adapter's own fetcher, not a hand-rolled client, because Part 4's thresholds were once
derived against a reimplementation of the scoring path and turned out to describe a
configuration the pipeline never ran.

### F20 — SSO's `<title>` separates three states; length only corroborates

| Page | Status | Bytes | `<title>` |
|---|---|---|---|
| `Act/IA1959` | 200 | 345,880 | `Immigration Act 1959 - Singapore Statutes Online` |
| `Act/PC1871` | 200 | 913,524 | `Penal Code 1871 - Singapore Statutes Online` |
| `Act/ZZZ9999` | 200 | 24,693 | `Page Not Found - Singapore Statutes Online` |
| `Act/NotARealAct2099` | 200 | 24,701 | `Page Not Found - Singapore Statutes Online` |
| WAF refusal | **403** | 919 | `ERROR: The request could not be satisfied` |

The same shape as F3/F12 on a different site: **a fabricated Act returns HTTP 200**, so
the status code carries no signal and the title carries it all.

```
title contains "page not found" AND the site name  -> NOT_FOUND   (the only FAIL)
title contains the site name, and does not         -> FOUND
anything else -- no title, a CDN error, empty body -> UNAVAILABLE (WARN, never FAIL)
```

The third row is why the WAF page was captured deliberately rather than inferred. A rule
separating only "real" from "not found" classifies a CloudFront refusal as a fabrication,
and every SSO citation reads as hallucinated for as long as the block lasts. That is F12
exactly, arriving at a second source. All three fixtures are in `tests/corpus`.

Byte size corroborates (24.7 kB against 346–913 kB) and is deliberately not branched on,
the same rule `L1_SOFT_404_MAX_BYTES` already states for eLitigation.

### F21 — a headless browser is the one client SSO refuses

The adapter first declared `FetchStrategy.BROWSER`, on the reasoning that an
`HTTP 202 / x-amzn-waf-action: challenge` meant the source was browser-only. Measured:

| Client | Result |
|---|---|
| `sal-verifier/0.1 (SMU LIT 2026 research prototype)` | **403** blocked |
| `Mozilla/5.0 (compatible; sal-verifier/0.1; SMU LIT 2026 research prototype)` | **200**, 346 kB |
| Playwright Chromium, `HeadlessChrome/151.0.7922.34` | **403** blocked |
| ~12 requests in quick succession | **202**, `x-amzn-waf-action: challenge` |

Both halves of the original reasoning were wrong. The challenge was **rate-based** and
cleared on its own within the hour; and the browser path was strictly worse than httpx,
because headless Chromium is refused outright. What the WAF wants is a conventionally
shaped user agent and a polite request rate — both of which we can give it honestly, since
the `(compatible; …)` form still names the tool and the project.

Two consequences worth stating plainly. First, `SOURCE_USER_AGENT` cannot be one global
value: eLitigation accepts the bare form and SSO answers it with 403, so the user agent is
now per-source (`SSO_USER_AGENT`). Second, nothing was done about the headless block. A
site refusing automated browsers is a preference to respect rather than an obstacle to
route around, and the plain-HTTP path needs no workaround.

The browser fetcher survives this finding because it was never really about SSO: it exists
for LawNet, which is login-walled and has no HTTP path at all. Wiring it up did find two
real defects in it — see `todo.md`.

### F22 — SSO serves a table of contents, not an Act

`Act/IA1959` is 346 kB of which **1,968 characters are statutory text**: a 106-entry table
of contents and four provisions. The rest of the Act is fetched by the page's own
JavaScript, and `?WholeDoc=1` — the target of the site's own "Whole Document" button —
returns the identical four.

This is the sharpest finding of the three, because the naive handling is catastrophic
rather than merely wrong. A `SourceDocument` built from that HTML contains sections 1–4;
quote-checking section 57 against it scores near zero and emits `QUOTE_NOT_FOUND`, which
is a FAIL. **A real statute, quoted correctly, reported as fabricated.**

`SsoAdapter.document_for` therefore returns `None`. L1c's "no document" branch is silence
rather than a finding, and L3 returns NOT_APPLICABLE, so the failure cannot occur. The
honest description of SSO coverage today is: **an Act can be confirmed to exist, and can
never be checked for what it says.** See `todo.md` bug 17.

---

## Part 8 — the live end-to-end run against eLitigation

Run on 2026-09-05 from 15:31 to 16:00 SGT, once eLitigation came back
(`200`, 153,370 bytes for `2007_SGCA_37`, against the ~848-byte maintenance page of
F12). The stack was all-real and unmodified: `PROVIDER_MODE=real`, no `*_MODE` reading
`mock` in either `api` or `worker`, `docker-compose.mockfetch.yml` not loaded, and the
running image verified byte-identical to `HEAD` by comparing an `md5` of every `.py`
under `src/` inside the container against the working tree. The database was empty at
the start (0 resolutions, 0 documents, 0 runs), so nothing below is served from a
pre-existing cache.

### F23 — the source registry dispatches correctly against the live site

This is what the run existed to test, and it passes. Four Singapore neutral citations
were resolved through `sources/registry.py`, each to a real judgment:

| `citation_key` | type | status | method | conf | document | chars |
|---|---|---|---|---|---|---|
| `sgca:2007:37` | neutral | resolved | search | 1.0 | yes | 112,762 |
| `sgca:2013:29` | neutral | resolved | url | 1.0 | yes | 130,864 |
| `sgca:2021:28` | neutral | resolved | url | 1.0 | yes | 19,929 |
| `sghc:2020:32` | neutral | resolved | url | 1.0 | yes | 14,885 |

Every row carries `domain = www.elitigation.sg` and `fetch_strategy = http`, so the
adapter was selected by citation type and its fetcher resolved from the declared
strategy, which is exactly the drop-in `tests/pipeline/test_mock_mode_resolution.py`
proves offline. Worker logs show live calls to `elitigation.sg`, `openrouter.ai` and
`api.voyageai.com` in the same run, so no capability silently fell back to mock.

The last three rows are the same `[2013] SGCA 29`, `[2021] SGCA 28` and `[2020] SGHC 32`
that bug 8 recorded being reported to a user as *"3 fabricated citations"* under the
mock-fetcher override. Against the live source they resolve, with documents. That
closes the loop on bug 8's diagnosis: the override was the defect, not the pipeline.

### F24 — the pipeline is correct, and does not fit in its own time budget

Executed in-process, with no Celery time limit, on the Spandeck answer:

| | |
|---|---|
| L0 extract | pass, 1,978 ms |
| L1 existence | **pass, score 1.0**, 14 ms |
| L2 source trust | pass, 0 ms |
| L3 grounding | **pass, score 0.537**, 43,392 ms |
| L4 responsiveness | pass, score 0.745, 7,346 ms |
| L5 judge | **pass, score 1.0**, 7,402 ms |
| verdict | **pass**, `$0.0338`, **53.4 s** |

L3 scores rather than returning NOT_APPLICABLE, and its top passage is Spandeck [72] at
0.744 — the paragraph that actually states the single-test holding — labelled with a
`paragraph`/`paragraph_to` range and a live `source_url`, which is F13's fix working on
real data. No `CITATION_NOT_FOUND` on any real case.

**But 53.4 s does not fit in `RUN_SOFT_LIMIT = 45`.** Through Celery the same work is
killed by `SoftTimeLimitExceeded` every time — observed on 4 of 4 runs dispatched through
the API. The pipeline is right; the budget it is given is wrong.

### F25 — the durable embedding cache is never wired in, so no run is ever warm

Measured, after a **fully successful** run:

```
chunks              0
text_embeddings     0
document_summaries  0
cache          hits 0   misses 171
```

`PgEmbeddingRepo` is implemented and constructed by `build_pg_repos()`, and nothing ever
reads it. `layers/registry.py:build_layer` constructs `SourceGroundingLayer()` and
`ResponsivenessLayer()` **with no arguments**, so `embedding_repo` resolves through
`semantic/defaults.py:default_embedding_repo()`, which returns an
`InMemoryEmbeddingRepo`. Grepping the tree confirms `embedding_repo` is never passed
anywhere: the only occurrences are the defaults inside L3 and L4 themselves.

Three consequences, and they compound:

1. Every run re-embeds the whole judgment. That ~40 s is what makes F24's 53.4 s exceed
   a 45 s budget, so **the cache that would fix the timeout is the thing that is broken**.
2. L3's contrastive background pool is whatever one process has seen, not the corpus.
   Part 4's margin is calibrated against a pool that does not survive a restart.
3. The `$0.034` per run never amortises.

This is the same defect, and the same shape, as *"Documents never written to durable
storage — the cache claim was false beyond one process"* in the fixed table. That fix
landed for `documents`; it did not land for embeddings, chunks or summaries. Documents
now persist (4 rows above), which is what made the gap visible.

### F26 — the judge is never deferred, so `judgeworker` is dead weight

`api/deps.py:_dispatch` calls `task.delay(run_id)` with no `defer_judge`, which defaults
to `False`. So the branch at `worker/tasks.py:134` that sends
`TASK_JUDGE_VERIFICATION` to `QUEUE_JUDGE` is unreachable from the API, and the judge —
budgeted `JUDGE_SOFT_LIMIT = 90` on its own queue — instead runs inline inside the
deterministic task's 45 s. Confirmed live: `judgeworker` subscribes to `judge`, reports
ready, and never receives a task; `redis-cli llen judge` is 0 while a run is mid-flight.

Dispatching the judge by hand for a stalled run finalised it in one poll, which is the
positive control: the deferred path works and is simply never taken. This is bug 15's
shape — a profile-gated worker waiting on a queue nothing writes to — arriving at the
judge queue rather than the browser queue.

### F27 — a killed task leaves the run `pending` forever

There is no `except SoftTimeLimitExceeded` anywhere in `worker/tasks.py`. When the limit
fires, Celery marks the *task* `FAILURE` and the *run row* keeps whatever status it last
wrote — `pending` if the deterministic phase had not finished, `deterministic_ready` if
it had. `is_final` stays `false`, so `GET /v1/runs/{id}` reports the run as still
working, indefinitely. Three of the four API-dispatched runs are still `pending` in the
table.

The client is what conceals this: the panel has its own timeout and renders
**"The verification timed out."** So the failure is visible to a user but invisible to
the API, and the row is indistinguishable from a run still in progress.

### F28 — a truncated embeddings response takes L3 down with no retry

One run in four died differently:

```
L3 could not complete: Response payload is not completed:
<ContentLengthError: 400, message='Not enough data to satisfy content length
header (received 27699 of 707298 bytes)'>
```

A Voyage response was cut off at 4% of its declared length. There is no retry, so a
single truncated batch errors the whole layer. The verdict degraded to `warn` with a
`LAYER_ERROR` finding rather than failing anything, which is the safe direction and is
the F12 rule holding — but L3 contributed nothing to that run, and the run still cost a
judge call.

### F29 — the extension verifies the sidebar, and calls it a FAIL

Driving the panel on live claude.ai produced 14 runs of `complete | fail` whose
`ai_output` is not an answer at all:

```
"Probability problem answer verification  Aug 22"
"Continent selection requirement  Aug 24"
"Runtime error in two-sum cement bag solution  Aug 26"
```

Those are **sidebar conversation titles with their dates**. The structural selector tier
finds the recents list's repeated group and classifies it as an assistant turn. todo.md
records this for `/new` and calls it cosmetic; it is neither. It fires on `/recents`
too, and a run over a 40-character title finds no citations and no grounding, which
resolves to a hard **FAIL** — the single worst verdict the system can emit, on text the
user never asked about.

The real answer was captured correctly in the same session (`[2007] SGCA 37`, Spandeck),
so the capture path works; the tier ladder just does not reject non-answers before
spending a run on them.

### What this run establishes, and what it does not

Established: the registry dispatches by citation type against the live source; adapters
resolve their fetcher from `fetch_strategy`; documents reach `LayerInput.documents`; L1
passes a real citation, L3 scores it, L5 runs; and a correct Singapore answer verifies
**pass** end to end for $0.034.

Not established: any of it through the deployed worker. F24–F27 mean that as shipped,
every cold run through the API or the extension times out. The one clean `pass` above
was obtained by running the pipeline in-process, outside Celery's limit, which is a
measurement of the pipeline and explicitly not a measurement of the product.

`n = 1` judgment and one answer. Enough to confirm the registry and to condemn the time
budget; not a benchmark.

---

## Part 9 — the fixes, measured through the deployed worker

Same stack, same answer, same day. The image was rebuilt from `2607f5a` and verified
byte-identical to the working tree before the run. Postgres and Redis were **not**
wiped, so the resolutions and documents from Part 8 were already present — this measures
the fixes, not a fresh install.

### F30 — the durable embedding cache works, and does not move the score

| | Part 8 (before) | Run A (cold) | Run B (warm) |
|---|---|---|---|
| `text_embeddings` rows | **0** | 170 | 170 |
| cache hits / misses | 0 / 171 | 0 / 171 | **170 / 1** |
| deterministic phase | 46.0 s | 61.9 s | **11.5 s** |
| L3 | 43.4 s | 59.8 s | **9.4 s** |
| total | killed at 45 s | 79.1 s | **26.5 s** |
| verdict | *never finished* | pass | pass |
| **L3 score** | 0.537 | **0.548** | **0.548** |

The row that matters most is the last one. A cache that changed the score would be a
bug wearing a speedup's clothes; the warm run and the cold run agree to three decimals,
because the key is the content hash of the embed input and the vectors are normalised
on write. `text_embeddings` had been 0 after every previous run, including a fully
successful one, so a non-zero count is the whole of F25's claim reversed.

Run A is *slower* than Part 8's 46.0 s because it now also pays to persist 170 vectors,
and because it ran against a busier machine. That cost is paid once per judgment.

### F31 — the judge runs on the queue that was built for it

`judgeworker` received its first task ever:

```
Task verifier.judge_verification[dc200487...] received
Task verifier.judge_verification[dc200487...] succeeded in 16.5s:
  {'status': 'complete', 'verdict': 'pass', 'judge_ran': True}
```

Both runs passed through `deterministic_ready` before reaching `complete`, which is the
deterministic verdict being published and rendered while the judge is still cold — the
behaviour the two-phase design was written for and had never once exhibited.

### F32 — a killed run now settles, verified against real Postgres

The offline tests exercise this against a fake repo, so it was also driven against
`PgRunRepo` in the worker container:

```
before: status=pending  is_final=False
after : status=error    is_final=True
        errors=['verification exceeded its 150s budget']
again : errors=1                      # idempotent under acks_late redelivery
```

and `GET /v1/runs/{id}` reports `status=error, is_final=True`, which is what stops the
extension polling. F27's three-of-four `pending` runs cannot recur.

### F33 — embeddings never carry a `document_id`, so the background pool stays empty

Newly visible, and not fixed. All 170 cached vectors have `document_id = NULL`:

```
embeddings with doc: 0 of 170
```

`_document_key` (`l3_alignment.py:645-656`) falls back to the document's **content hash**
when `SourceDocument.id` is unset, which it is for a freshly-fetched document.
`PgEmbeddingRepo.put_many` does `uuid.UUID(document_id)` and, on `ValueError`, stores
`NULL`. `sample_background` then filters `document_id IS NOT NULL` and matches nothing.

L3 detects it and says so, which is the system behaving correctly:

```
background_empty: true, margin_skipped: true
note: "No background corpus was available, so the contrastive margin was
       skipped and only the absolute floor was applied."
```

**This is not a regression.** Before the fix the in-memory repo started cold in every
task and had only ever seen the one document under test, so the pool was empty then too.
What changes is that it is now *permanently* empty rather than incidentally so: 170
durable vectors are sitting in the table and none can be sampled. The contrastive margin
Part 4 calibrates has therefore never run in production — every live L3 verdict to date,
including Part 8's and both runs above, rests on the absolute floor alone.

That does not invalidate the scores, which are `max cos(claim, cited)` and independent of
the margin. It does mean the margin half of L3 is unmeasured outside the offline
calibration set, and it is recorded as todo.md bug 24.
