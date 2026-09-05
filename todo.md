# TODO

Open work, highest value first. Anything already fixed is recorded at the foot for
context, not as outstanding work.

---

## Bugs to fix

### 1. The contextual prefix fails correct legal work — CONFIRMED, not fixed

**Severity: high — measured failing a correct answer on a live run.**

No longer a suspicion. See `docs/03-findings.md` F14.

**Live.** A correct answer citing `[2007] SGCA 37` and quoting paragraph [115]
verbatim: L1 scored the quote **1.000**, L4 scored **0.751**, and **L3 failed it** at
0.325 against the 0.35 floor. The failing claim was the quoted sentence itself, and the
chunk containing [115] was not retrieved at all.

**A/B against real `voyage-law-2`**, four grounded claims, 43 chunks, prefix on vs raw:

| | Prefixed (as shipped) | Raw |
|---|---|---|
| Mean max cos | 0.503 | **0.621** |
| Claims below the 0.35 floor | **1 of 4** | **0 of 4** |
| The quoted paragraph's own chunk | 0.304, rank **#11** | **0.431**, rank **#4** |

The summary is 1,370 chars (~342 tokens) and is byte-identical across all 43 chunks, so
it adds a large shared component to every document vector with no counterpart in the
query vector — claims are embedded bare. It both lowers absolute similarity and
compresses the differences *between* chunks, which is why it damages L3's verdict and
L5's evidence by the same mechanism.

**Still do not fix this by lowering the threshold.** The raw-text figures are the ones
Part 4 calibrated, and they fail 0 of 4.

**The fix, when it is taken:** stop prefixing on the L3 document path. Note there is no
switch for it today — `get_document_summary` returns `""` only when `summariser is
None`, and no production path produces that, so the "config 1 vs config 2" A/B in the
old plan was never runnable in-process.

**Correction to what this file used to say:** `EmbeddingsProvider.contextualized_embed`
does not exist. `VoyageEmbedder.contextualized_embed` and `uses_native_context` do, and
have **zero call sites in `src/`** — `CachedEmbedder._embed` always calls `embed()` and
`_embed_source` always prefixes. `EMBEDDINGS_MODEL=voyage-context-4` would today send
chunks to the ordinary endpoint *and still prefix them by hand*. The one-env-var A/B is
unimplemented, and that model has no calibrated thresholds either.

Files: `src/verifier/semantic/contextualise.py`, `src/verifier/layers/l3_alignment.py`,
`src/verifier/providers/voyage.py`.

---

### 2. ~~The judge's verdict tracks whatever passages L3 hands it~~ — FIXED

Diagnosed to three concrete causes, none of which was the passage cap this file
originally proposed widening. See `docs/03-findings.md` F13.

- **Retrieval was top-1 per claim.** `best_match` is `top_k(k=1)`, so the candidate
  pool was one passage per (citation × attributed claim). `MAX_JUDGE_PASSAGES = 12`
  was never the binding constraint and widening it would have changed nothing.
- **Passages were truncated at 1,800 chars by byte offset.** 22 of Spandeck's 43
  chunks exceed that (median 2,042, max 7,103), so for half the corpus the judge read
  the opening quarter of a passage — a decisive paragraph could be retrieved correctly
  and still never reach it.
- **Provenance was wrong.** A passage was labelled `chunk.paragraph_from`, the first
  paragraph of a merge, so `at [187]` could head text from [188]–[190].
- **The harvest cap applied to arrival order**, and the orchestrator supplies L1 before
  L3, so incidental quote evidence could displace what L3 actually ranked.

Fixed: top-k per claim; an over-long chunk split into its own numbered paragraphs and
ranked; passages labelled with the range they cover; the budget spent round-robin so
every attributed claim is represented before any gets depth; the harvest ranked by
score. Live, the judge's evidence went from 5 passages to **21**.

**The score is untouched** — L3 still scores `max cos(claim, chunks)`, so Part 4's
thresholds stand. `test_widening_retrieval_does_not_move_the_score` pins it.

Retrieval coverage now rides in `LayerResult.detail["retrieval"]` (claims attributed vs
total, passages generated vs kept, best dropped score, whether a split was applied), so
a thin evidence set is visible in the panel rather than inferred from a confident
verdict arriving with nothing behind it.

The honest framing for the writeup is unchanged and now demonstrated: the deterministic
layers are model-independent, but **L5's reliability is bounded by retrieval quality**.

---

### 3. The Chrome extension does not inject — code defects fixed, load pending

**Severity: high for the demo.**

Ruled out by direct inspection: the manifest parses with no BOM, all 11 referenced
paths exist case-correctly, the icons are valid PNGs at their declared sizes, all seven
JS files pass `node --check`, there is no ES-module syntax anywhere, load order is
correct, and there is no build step to have skipped. CORS already allows
`https://claude.ai` and `chrome-extension://`.

Three real defects, now fixed:

1. **`background.js` still hardcoded `http://localhost:8000`.** Commit `402f537` fixed
   `config.js` and missed the proxy. `api.js` falls back to the background worker the
   first time a direct fetch fails and **the fallback is sticky**, so from that moment
   every request went to `localhost` → `::1` while uvicorn was bound to `127.0.0.1` —
   the bug that was already fixed, surviving on the path where it presents as an
   intermittently dead backend.
2. **`boot()` awaited `SALV.loadConfig()` before `panel.mount()`.** In an orphaned
   content script — the extension reloaded while a claude.ai tab stayed open, which
   happens on every edit — `chrome.storage.sync.get` **never settles**. It does not
   reject, so `try/catch` catches nothing and boot parks forever: no panel, no error,
   no output. That is exactly the reported symptom, and the same signature as the
   page-context `fetch` that "hung rather than resolving or rejecting". The panel now
   mounts before anything is awaited, and the storage read races a 1 s timeout.
3. **Diagnosis was blocked by invisible logging.** `SALV.log` used `console.debug`,
   which Chrome hides unless the level is set to Verbose — so "no console output" was
   never evidence of anything. `SALV.banner` (`console.info`) now announces the script
   at boot.

Also widened `matches` and the context menu to `https://*.claude.ai/*`.

**Remaining:** Chrome 152 removed `--load-extension` and `chrome://` pages cannot be
driven by automation, so the extension has to be loaded by hand:
`chrome://extensions` → Developer mode → **Load unpacked** → the `extension/`
directory (the one holding `manifest.json`). Hard-reload the claude.ai tab afterwards —
content scripts do not retro-inject into tabs that were already open.

Files: `extension/src/background.js`, `extension/src/content.js`,
`extension/src/config.js`, `extension/manifest.json`.

---

### 4. `make seed-lists` reports success while writing nowhere durable

**Severity: medium — cosmetic today, misleading tomorrow.**

`repos/lists.py` was never implemented. `repos/pg.py` was written to tolerate its
absence and falls back to `InMemoryListRepo`, so `make seed-lists` prints "Seeded 37
source trust entries" and the rows vanish with the process. `GET /v1/lists` returns
`[]` on a Postgres backend.

L2 is unaffected — it lazily builds its own seeded in-memory repo — so this is not a
correctness bug in the verdict. But the list-management API is inert, and a claim of
persistence that is false is the same shape as the document-cache bug already in the
fixed table below.

Files: `src/verifier/repos/lists.py` (missing), `src/verifier/repos/pg.py`.

---

## QoL improvements

### The panel is unreadable in dark mode

**Severity: medium — the verdict is correct and nobody can read it.**

Found while driving the extension on live claude.ai. `panel.css` defines the whole
palette as a light theme — 32 hardcoded `color:` rules — and its
`@media (prefers-color-scheme: dark)` block overrides only **nine** selectors, almost
all of them backgrounds. Every text colour therefore keeps its light-mode value on a
dark panel. Measured in the browser against the panel's own `#191b21`:

| Element | Size | Contrast | WCAG AA (4.5:1) |
|---|---|---|---|
| `.salv-finding-msg` — *the finding itself* | 12px | **1.05** | ✗ |
| `.salv-section-title` ("LAYERS") | 11px | 2.19 | ✗ |
| `.salv-code` (`CLAIM_NOT_GROUNDED_IN_SOURCE`) | 10px | 2.69 | ✗ |
| `.salv-summary` (timing, cache) | 11px | 3.57 | ✗ |
| `.salv-section-note` | 11px | 4.17 | ✗ |
| `.salv-layer-meta` / `.salv-duration` / `.salv-score` | 10px | 4.48 | ✗ |
| `.salv-layer-name` | 12px | 14.31 | ✓ |
| `.salv-shortcircuit` | 12px | 8.67 | ✓ |

Eight of ten sampled styles fail AA, and the worst is the one that matters most:
`.salv-finding-msg { color: #23262e }` on `#191b21` is **1.05:1** — the sentence
explaining *why* an answer was failed is effectively invisible. In the live run the
panel correctly reported "Nothing in [2007] SGCA 37 closely matches this claim (best
passage similarity 0.326, floor 0.35)" and a reader simply could not see it.

That is worse than an ugly panel. This tool exists to tell a lawyer why an answer was
rejected; a verdict nobody can read is a verdict that will be ignored, and an accuracy
tool that gets ignored has failed at the only thing it does.

**The fix:** replace the hardcoded hexes with CSS custom properties on `#salv-panel`,
and redefine only those variables inside the dark block — so a colour can never again
be defined in one theme and forgotten in the other. Re-check every token against 4.5:1,
and raise the 10px metadata sizes while there. Not a repaint; a token pass.

Files: `extension/src/panel.css`.

### Smaller things spotted in the same pass

- **The panel shows an error on an empty chat.** On `/new` the structural tier finds
  the sidebar's repeated group and calls half of it an assistant turn, so the panel
  renders "could not find the question this response answers" where it should say
  idle. Cosmetic, but it is the first thing a demo audience sees.
- **Long evidence passages are not scrollable.** A retrieved passage renders at full
  height inside the finding, pushing the rest of the report below the fold. A
  `max-height` with `overflow-y: auto` would keep the layer table in view.

## Calibration debt

- **Widen the threshold samples.** L4 is calibrated on `n=11`, L3 on `n=5`. Enough to
  replace a demonstrably wrong threshold with a measured one; not enough to quote a
  confidence interval. See `docs/03-findings.md` Part 4.
- **L1's quote bands are still uncalibrated under `partial_ratio`** at scale — the
  75/90 figures separate the regimes cleanly on one judgment, which is not the same as
  being right across the corpus.
- **Every number is model-specific.** Changing `EMBEDDINGS_MODEL` invalidates all of
  them.

## Deferred by scope decision

- **Bias evaluation** — the problem statement asks for it. It would attach as an L5
  rubric dimension plus a deterministic authority-balance signal, and the citation
  graph it needs is already extracted free from `<nobr>` tags into
  `documents.cited_authorities`.
- **A labelled benchmark harness** — ~50 (question, answer) pairs, half poisoned with
  known defect types, scored for precision/recall per layer. This is what would turn
  "the verifier works" into a measured claim.

---

## Fixed already (context, not outstanding)

| | Found by |
|---|---|
| `--load-extension` service worker misattributed to ours | correcting an earlier wrong claim |
| Citation resolution silently disabled by a one-character casing typo swallowed by a broad `except` | live run |
| Parser losing two-thirds of every judgment to `id(node)` collisions | cross-stream test |
| Maintenance page classified as a fabricated citation (F12), and again via zero search hits | live outage during the build |
| Query-side vectors poisoning their own background pool | code review during build |
| Compose requiring a `.env` the repo does not ship | `make dev` |
| `.env.example` pinning thresholds that defeat per-model calibration | `make dev` |
| `PROVIDER_MODE` coupling storage and vendor selection; then vendor capabilities to each other | `make dev`, then the OpenRouter key |
| Documents never written to durable storage — the cache claim was false beyond one process | real Postgres |
| `localhost` → `::1` while uvicorn bound `127.0.0.1` | loading the extension |
| The judge grading with no source passages at all | first real-model run |
| `response_format` overriding the judge prompt's own output contract | installing the user's prompt |
| L5 crashing on `None` scores for unpopulated rubric dimensions | installing the user's prompt |
| L4's 0.50 threshold failing 3 of 5 correct answers | real `voyage-law-2` |
| The judge given one passage per claim, truncated at a byte offset, labelled with the wrong paragraph | first full e2e |
| The harvest cap applied to arrival order, so L1 evidence displaced L3's ranking | reading the orchestrator's layer order |
| `background.js` still on `localhost` after the 127.0.0.1 fix — on the sticky proxy path | grepping the extension |
| `boot()` awaiting a `chrome.storage` read that never settles, before mounting the panel | tracing the injection symptom |
| Selector tier 1 classifying three action-bar buttons as user messages, and winning the ladder with no assistant in the result | driving the extension on live claude.ai |
| `dedupeNesting` keeping the outermost match, so the `aria-label="Chat messages"` wrapper displaced both real turns | same |
