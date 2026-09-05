# TODO

Open work, highest value first. Anything already fixed is recorded at the foot for
context, not as outstanding work.

---

## Bugs to fix

### 1. The contextual prefix is hurting L3 retrieval

**Severity: high — it currently fails correct legal work.**

Every chunk is embedded with a document summary and heading path prefixed to it, on
the theory that this gives each chunk global context. Measured against real
`voyage-law-2`, the prefix appears to be *diluting* the signal instead.

| | genuine claim | foreign claim |
|---|---|---|
| **Raw** paragraphs | min **0.501** | max **0.251** |
| **Prefixed** (live pipeline) | a genuinely grounded claim was **FAILED** | — |

On raw text the separation is wide and the current floor (0.35) fails 0 of 3 genuine
claims. In the live pipeline the same kind of claim fails. The summary text is long
relative to the chunk and plausibly dominates the embedding.

**Do not fix this by lowering the threshold.** That tunes around the bug and hides a
regression behind a green demo. The thresholds are measured and correct for raw text.

**Next step:** A/B three configurations over the same claim set —
1. raw chunks, no prefix
2. current summary + heading prefix
3. `EMBEDDINGS_MODEL=voyage-context-4` (Voyage's native contextualised endpoint,
   already wired behind `EmbeddingsProvider.contextualized_embed`)

Option 3 is what the provider interface was built for: it does the contextualisation
inside the model rather than by string concatenation. If it wins, the hand-built
prefix and the summariser call on the L3 path both disappear — which also removes a
Haiku call from the critical path.

Files: `src/verifier/semantic/contextualise.py`, `src/verifier/semantic/embed.py`,
`src/verifier/providers/voyage.py`.

---

### 2. The judge's verdict tracks whatever passages L3 hands it

**Severity: high — it is a correctness sensitivity, not flakiness.**

Across runs, L5 both **caught and passed the same wrong-holding answer**, following
the passages it was given. When L3 retrieved paragraph [115] the judge quoted it and
correctly failed the answer; when it retrieved different passages the judge passed the
same text.

This is not the judge being unreliable. It is doing exactly what it was told: reason
from the supplied evidence and do not fill gaps from memory. The weakness is upstream
— **L5 is only as good as L3's retrieval**, and nothing currently guarantees the
decisive passage is among the ones selected.

Worth noting this interacts with bug 1: better retrieval fixes both.

**Options, roughly in order of cost:**
- Widen `MAX_JUDGE_PASSAGES` (currently 12) and check whether recall of the decisive
  paragraph improves. Cheap, and measurable.
- Retrieve per *claim* rather than per document, so each claim contributes its own
  best passage rather than competing for slots.
- Add a lexical (BM25) retrieval pass alongside the vector one and union the results —
  the decisive paragraph often shares distinctive terms with the claim even when the
  embedding does not rank it first.
- Report retrieval coverage in `LayerResult.detail` so a thin evidence set is visible
  in the panel rather than silently producing a confident verdict.

The honest framing for the writeup: the deterministic layers are model-independent,
but **L5's reliability is bounded by retrieval quality**, and that bound should be
stated rather than discovered by a judge.

Files: `src/verifier/layers/l3_alignment.py`, `src/verifier/layers/l5_judge.py`.

---

### 3. The Chrome extension does not inject

**Severity: high for the demo — the UI overlay never appears.**

`#salv-panel` is absent on `claude.ai`, no `salv-*` node exists, no console output and
no errors. `boot()` calls `panel.mount()` unconditionally, so the content script
cannot be running at all.

Ruled out so far:
- Not a code error — nothing is thrown, and no content-script execution context is
  created at all (checked via CDP `Runtime.executionContextCreated`; only the page's
  own worlds appear).
- Not the API — it is up, and CORS is verified for both `https://claude.ai` and
  `chrome-extension://` origins.
- **Not `localhost` any more** — that *was* a real bug (`localhost` resolves to `::1`
  first while uvicorn binds `127.0.0.1`), fixed in #2. But the extension is not
  reaching the network stage at all.
- CLI loading is a dead end: Chrome 152 restricts `--load-extension` (removed for
  security in 137+), and `--disable-extensions-except` stopped it loading entirely.

**Next step:** confirm in `chrome://extensions` that *SAL Verifier* is listed, toggled
**on**, and shows no error badge, then hard-reload the claude.ai tab. If it is listed
and enabled, open its service-worker console from that page and reload — the failure
is then in `content.js` before `panel.mount()`, most likely `SALV.loadConfig()`
awaiting a `chrome.storage` call that never resolves.

A page-context `fetch` to the API also **hung** rather than resolving or rejecting,
which is worth re-testing once the extension is confirmed loaded — a hang is not the
signature of a CSP block (which rejects immediately) and may be a second issue.

Files: `extension/src/content.js`, `extension/src/config.js`, `extension/manifest.json`.

---

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
