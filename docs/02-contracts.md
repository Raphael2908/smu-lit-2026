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
| `layers/l1_existence.py`, `layers/l2_lists.py`, `repos/lists.py`, `repos/seed_lists.py` | Stream B | |
| `semantic/**`, `layers/l3_*.py`, `layers/l4_*.py`, `providers/voyage.py`, `providers/mock/embeddings.py` | Stream C | |
| `layers/l5_judge.py`, `layers/prompts/**`, `pipeline/**`, `worker/**`, judge providers | Stream D | |
| `repos/` (Postgres), `api/**`, `extension/**` | Stream E | |

`providers/factory.py` and `layers/registry.py` were written with **all** branches up
front. Each stream fills in one line rather than adding structure — that is why they
merge cleanly despite being touched by everyone.

## Three contract decisions that are load-bearing

### 1. `ExtractedQuote.delimiter` is required

Not decoration. Lexical matching is *anti-correlated* on paraphrase: a genuine
paraphrase scores **lower** than a fabrication. So L1 may only ever score text that was
actually presented as a direct quotation, and paraphrased attributions belong to L3.
Making `delimiter` required means the type system enforces what a comment would not.

### 2. `Finding.source` separates ground truth from opinion

`deterministic` vs `llm`. The extension renders them differently, under a labelled
"LLM judge (advisory)" section. That visual separation is the user-facing form of the
invariant that the judge cannot clear a deterministic failure.

### 3. `Verdict` is an ordered lattice, and `PENDING` is excluded from it

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
