# The frozen contract

Everything in `src/verifier/contracts/` is frozen. Every module compiles against these
types, which is what lets independent workstreams proceed without coordinating.

**Changing anything here is a contract change.** Announce it, and regenerate the
snapshot — `tests/contracts/test_schema_snapshot.py` fails the build otherwise, which
deliberately converts a silent merge disaster into a loud red test:

```bash
uv run python -m tests.contracts.test_schema_snapshot   # regenerate deliberately
```

## Ownership map

| Area | Owner | Notes |
|---|---|---|
| `contracts/**`, `settings.py`, `*/base.py`, `alembic/**` | shared, frozen | changes require announcement |
| `extraction/**`, `sources/**`, `providers/fetcher_http.py`, `providers/mock/fetcher.py` | Stream A | |
| `layers/l0_preprocessing.py`, `layers/l1_citation_integrity.py`, `layers/l1a_existence.py`, `layers/l1b_lists.py`, `extraction/propositions.py`, `repos/lists.py`, `repos/seed_lists.py` | Stream B | |
| `semantic/**`, `layers/l2_alignment.py`, `layers/l3_responsiveness.py`, `providers/voyage.py`, `providers/mock/embeddings.py` | Stream C | |
| `layers/l4_judge.py`, `layers/prompts/**`, `pipeline/**`, `worker/**`, judge providers | Stream D | |
| `repos/` (Postgres), `api/**`, `extension/**` | Stream E | |

`providers/factory.py` and `layers/registry.py` were written with **all** branches up
front. Each stream fills in one line rather than adding structure — that is why they
merge cleanly despite being touched by everyone.

## Contract changes

### 2026-09-07 — `Layer.L0_PREPROCESSING`, `SubLayer` renumbered, `LayerInput.claims`

**Breaking.** Three changes to frozen contracts, all from one move: citedness left L1
for L0.

`Layer.L0_EXTRACT` → **`Layer.L0_PREPROCESSING`** (wire value `"L0"` unchanged). It is no
longer only an extraction pre-pass: it carries a verdict and can end a run, so the name
that said "extract" was describing half of it.

`SubLayer` loses a member and renumbers the two that remain:

| Before | After | Question |
|---|---|---|
| `L1A_CITEDNESS` = `"L1a"` | *removed* — now L0's gate | is anything cited at all? |
| `L1B_EXISTENCE` = `"L1b"` | `L1A_EXISTENCE` = `"L1a"` | does this citation exist? |
| `L1C_SOURCE_TRUST` = `"L1c"` | `L1B_SOURCE_TRUST` = `"L1b"` | is the source trusted? |

The wire values `"L1a"` and `"L1b"` therefore **stay valid and change meaning**, which is
why `alembic/0003` deletes historical runs rather than remapping them, and why
`repos/runs.py` now degrades an unrecognised stored sub-layer to `None` instead of
raising: a missing caption costs a reader one line, a `ValueError` on read costs the
whole run.

New `FindingCode.PREPROCESSING_FAILED`, and `OUTPUT_UNCITED`/`PROPOSITION_UNCITED` now
carry `layer = "L0"` with `sub_layer = None`.

**`LayerInput.claims: tuple[RawChunk, ...]`** — the AI output split into claims once, by
L0, for L2 and L3 to share. `RawChunk` moved from `semantic/chunking.py` to
`contracts/chunks.py` for this (`contracts` may not import `semantic`);
`semantic.chunking` re-exports it, so no existing import changed.

Settings: `L1A_ENABLED` → `L0_CITEDNESS_ENABLED`, `L1A_UNCITED_SEVERITY` →
`L0_UNCITED_SEVERITY`, `L1A_MIN_ASSERTIONS_FOR_FAIL` → `L0_MIN_ASSERTIONS_FOR_FAIL`.

**Behaviour change, not just naming:** a degraded extractor now FAILS the run. See
`docs/01-architecture.md` and `todo.md` bug 5 for the cost.

### 2026-09-05 — `ExtractionResult.untyped` and `.extractor_degraded` (L0's extractor)

**Added:** `ExtractionResult.untyped`, `ExtractionResult.extractor_degraded`,
`providers/base.CitationCandidate`, `CitationExtraction`, `CitationExtractor`,
`settings.EXTRACTOR_MODE` / `EXTRACTOR_MODEL` / `EXTRACTOR_TIMEOUT_S` /
`EXTRACTOR_PROMPT_VERSION`.

**Nothing was removed or renamed.** Both new fields default to empty, so every existing
consumer compiles unchanged. `authority_count` now also counts `untyped`.

Why they are on the contract rather than inside L0. **`extractor_degraded`** is the one
that matters. The gate's FAIL asserts "this output cited nothing", which is only a claim about
the output if something actually looked; once the citations come from a model, a timeout
and an uncited answer produce the same zero. L1 is pure with respect to `LayerInput`, so
the only way it can tell them apart is a field on the extraction, and without it an
extractor outage would be reported to a lawyer as a fabrication.

**`untyped`** holds authority the parser cannot type — an unenumerated report series, a
practice direction, a textbook. It counts for L0's gate and is deliberately never clustered:
resolving one means searching a Singapore judgment corpus for a phrase that is not in
it, and zero hits is exactly what this system reads as fabrication (F6). A separate
field rather than a new `CitationType` member, because three dicts index `citation_type`
without a default (`CitationCluster.preferred`, `l1a_existence._TYPE_ORDER`,
`ElitigationAdapter.resolve_cluster`) and a fourth member reaches them as a `KeyError`.

### 2026-09-05 — `ExtractionResult.propositions` and `.statutes` (L0's gate)

**Added:** `ExtractedProposition`, `StatuteReference`, `PropositionKind`,
`AuthorityKind`, `AttributionMethod.CARRIED`, `FindingCode.OUTPUT_UNCITED`,
`FindingCode.PROPOSITION_UNCITED`, `ExtractionResult.propositions`,
`ExtractionResult.statutes`, `ExtractionResult.authority_count`.

**Nothing was removed or renamed**, so every existing consumer compiles unchanged; the
new fields default to empty. The snapshot was regenerated and now covers the two new
models.

Why it landed on the contract rather than inside L1: propositions are L0's output, and
L1 is pure with respect to `LayerInput` like every other layer. Putting them on
`ExtractionResult` also makes them visible to the panel and to L4, which is what lets
the judge take over the attribution question L0 deliberately refuses to decide.

## Four contract decisions that are load-bearing

### 1. `ExtractedQuote.delimiter` is required

Not decoration. Lexical matching cannot separate a genuine paraphrase from a fabrication:
the two land 3.6 points apart, and which one scores higher does not even reproduce across
measurements (`docs/03-findings.md` Part 3, `docs/v1-plan.md` F8). That is why the
quote-verification check was eventually removed altogether — a 75/90 band sitting 25
points above both regimes was deciding FAIL on noise.

The type survives the check that motivated it, and `delimiter` stays required, because
two things still turn on knowing a span was presented as a quotation: L2 attributes a
claim to the citation whose quotation it overlaps, and L0 MASKS quoted text before
deciding which sentences are the answer's own assertions of law. Drop the delimiter and
every quoted sentence starts counting as an uncited assertion.

### 2. `Finding.source` separates ground truth from opinion

`deterministic` vs `llm`. The extension renders them differently, under a labelled
"LLM judge (advisory)" section. That visual separation is the user-facing form of the
invariant that the judge cannot clear a deterministic failure.

### 3. L0 splits "no authority anywhere" from "no authority *here*"

`OUTPUT_UNCITED` can FAIL; `PROPOSITION_UNCITED` never can. The split is not a
severity preference, it is a statement about what each finding knows.

`authority_count == 0` is a **count over the whole output**. It contains no attribution
judgement, so there is nothing in it to be wrong about beyond whether the text contains
a citation at all — which is why it can carry a verdict that skips the judge, exactly
as L1's 1a `CITATION_NOT_FOUND` does.

Deciding *which* citation supports *which* sentence is a different kind of claim.
Authority may precede its proposition, follow it, or sit once at the head of a
paragraph that discusses it for five sentences, and no rule over prose gets that right
every time. So coverage is deliberately generous (`AttributionMethod.CARRIED` clears
everything after a citation in its scope), per-proposition findings only WARN, and the
residue is handed to L4's `citation_integrity` dimension — where a reasoning model may
convict on it, labelled `llm`, and still cannot acquit.

### 4. `Verdict` is an ordered lattice, and `PENDING` is excluded from it

`VERDICT_ORDER` covers FAIL < WARN < PASS only. `PENDING` is a lifecycle state, not a
verdict; letting it into a comparison would silently mask aggregation bugs, so it is
absent and a lookup on it raises.

## Layer contract

Every layer subclasses `BaseLayer` and implements `async _run(LayerInput) -> LayerResult`.
Layers are **pure with respect to `LayerInput`**: they never reach into the database or
another layer's state.

`BaseLayer.run` maps an unhandled exception to `LayerStatus.ERROR`, never `FAIL`.
Failing a lawyer's output because our own code broke is the worst available false
positive.

## Provider contract

Orchestration depends only on the protocols in `providers/base.py`. `factory.py`
returns mocks when `PROVIDER_MODE=mock`, else lazily imports the real implementation —
so the API tier never pulls a vendor SDK it will not use, and a real provider missing
its key raises `ProviderKeyMissing` at construction. **Never fall back to a mock
silently**: a verifier that quietly stops verifying is worse than one that stops.

`Embedder.embed` takes `input_type` (`"query"` | `"document"`). This asymmetry is
load-bearing — without it a short interrogative and a long passage do not land in the
same retrieval space, a perfect answer scores artificially low, and every threshold
derived from it is meaningless.

## Mock mode

`PROVIDER_MODE=mock` boots and passes the whole suite with **no keys and no network**
(`pytest-socket` enforces it).

The mock fetcher serves `tests/corpus/*.html`, so **L0's gate and both of L1's sub-checks
are fully real in mock mode** — only L2/L3/L4 are stubbed. The stages that produce
failures are the ones testable offline, which is the right split.

The mock embedder is a **hashed bag-of-words** vectorizer, not random vectors, so
threshold and margin logic is genuinely exercised.
