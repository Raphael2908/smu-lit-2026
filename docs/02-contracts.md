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
| `layers/l1_existence.py`, `layers/l2_lists.py`, `extraction/propositions.py`, `repos/lists.py`, `repos/seed_lists.py` | Stream B | |
| `semantic/**`, `layers/l3_*.py`, `layers/l4_*.py`, `providers/voyage.py`, `providers/mock/embeddings.py` | Stream C | |
| `layers/l5_judge.py`, `layers/prompts/**`, `pipeline/**`, `worker/**`, judge providers | Stream D | |
| `repos/` (Postgres), `api/**`, `extension/**` | Stream E | |

`providers/factory.py` and `layers/registry.py` were written with **all** branches up
front. Each stream fills in one line rather than adding structure — that is why they
merge cleanly despite being touched by everyone.

## Contract changes

### 2026-09-05 — `ExtractionResult.untyped` and `.extractor_degraded` (L1a's extractor)

**Added:** `ExtractionResult.untyped`, `ExtractionResult.extractor_degraded`,
`providers/base.CitationCandidate`, `CitationExtraction`, `CitationExtractor`,
`settings.EXTRACTOR_MODE` / `EXTRACTOR_MODEL` / `EXTRACTOR_TIMEOUT_S` /
`EXTRACTOR_PROMPT_VERSION`.

**Nothing was removed or renamed.** Both new fields default to empty, so every existing
consumer compiles unchanged. `authority_count` now also counts `untyped`.

Why they are on the contract rather than inside L0. **`extractor_degraded`** is the one
that matters. L1a's FAIL asserts "this output cited nothing", which is only a claim about
the output if something actually looked; once the citations come from a model, a timeout
and an uncited answer produce the same zero. L1 is pure with respect to `LayerInput`, so
the only way it can tell them apart is a field on the extraction, and without it an
extractor outage would be reported to a lawyer as a fabrication.

**`untyped`** holds authority the parser cannot type — an unenumerated report series, a
practice direction, a textbook. It counts for L1a and is deliberately never clustered:
resolving one means searching a Singapore judgment corpus for a phrase that is not in
it, and zero hits is exactly what this system reads as fabrication (F6). A separate
field rather than a new `CitationType` member, because three dicts index `citation_type`
without a default (`CitationCluster.preferred`, `l1_existence._TYPE_ORDER`,
`ElitigationAdapter.resolve_cluster`) and a fourth member reaches them as a `KeyError`.

### 2026-09-05 — `ExtractionResult.propositions` and `.statutes` (L1a)

**Added:** `ExtractedProposition`, `StatuteReference`, `PropositionKind`,
`AuthorityKind`, `AttributionMethod.CARRIED`, `FindingCode.OUTPUT_UNCITED`,
`FindingCode.PROPOSITION_UNCITED`, `ExtractionResult.propositions`,
`ExtractionResult.statutes`, `ExtractionResult.authority_count`.

**Nothing was removed or renamed**, so every existing consumer compiles unchanged; the
new fields default to empty. The snapshot was regenerated and now covers the two new
models.

Why it landed on the contract rather than inside L1: propositions are L0's output, and
L1 is pure with respect to `LayerInput` like every other layer. Putting them on
`ExtractionResult` also makes them visible to the panel and to L5, which is what lets
the judge take over the attribution question L1a deliberately refuses to decide.

## Four contract decisions that are load-bearing

### 1. `ExtractedQuote.delimiter` is required

Not decoration. Lexical matching is *anti-correlated* on paraphrase: a genuine
paraphrase scores **lower** than a fabrication. So L1 may only ever score text that was
actually presented as a direct quotation, and paraphrased attributions belong to L3.
Making `delimiter` required means the type system enforces what a comment would not.

### 2. `Finding.source` separates ground truth from opinion

`deterministic` vs `llm`. The extension renders them differently, under a labelled
"LLM judge (advisory)" section. That visual separation is the user-facing form of the
invariant that the judge cannot clear a deterministic failure.

### 3. L1a splits "no authority anywhere" from "no authority *here*"

`OUTPUT_UNCITED` can FAIL; `PROPOSITION_UNCITED` never can. The split is not a
severity preference, it is a statement about what each finding knows.

`authority_count == 0` is a **count over the whole output**. It contains no attribution
judgement, so there is nothing in it to be wrong about beyond whether the text contains
a citation at all — which is why it can carry a verdict that skips the judge, exactly
as L1b's `CITATION_NOT_FOUND` does.

Deciding *which* citation supports *which* sentence is a different kind of claim.
Authority may precede its proposition, follow it, or sit once at the head of a
paragraph that discusses it for five sentences, and no rule over prose gets that right
every time. So coverage is deliberately generous (`AttributionMethod.CARRIED` clears
everything after a citation in its scope), per-proposition findings only WARN, and the
residue is handed to L5's `citation_integrity` dimension — where a reasoning model may
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

The mock fetcher serves `tests/corpus/*.html`, so **L1 and L2 are fully real in mock
mode** — only L3/L4/L5 are stubbed. The layers that produce failures are the ones
testable offline, which is the right split.

The mock embedder is a **hashed bag-of-words** vectorizer, not random vectors, so
threshold and margin logic is genuinely exercised.
