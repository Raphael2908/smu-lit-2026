# Sigma Tech

Automated verification of legal AI outputs. Given a `(question, ai_output)` pair scraped from
claude.ai by a Chrome extension, four layers score it for citation integrity, semantic
alignment, responsiveness and factual faithfulness — returning a green check, or a red check
with the reason.

The first thing it asks is the one that is easy to forget: **does the answer cite anything at
all?** An output can be entirely free of fabricated citations by citing nothing whatsoever, and
every later check only ever examines authority the answer actually offered.

Built for SMU LIT 2026, Problem Statement 2 (Singapore Academy of Law).

## Why it is defensible

**The LLM judge can convict but never acquit.** Layers 1–3 are deterministic and run first; if
any fails, the run fails and the judge is never consulted. The judge only ever sees output that
already passed every machine-checkable test. That invariant is the answer to "who audits the
auditor?", and it is enforced in `pipeline/aggregate.py` and proven by
`tests/pipeline/test_judge_cannot_launder.py`.

## The layers

**L1 is one layer that asks its question in three parts.** They are reported as sub-checks on
the layer's result, not as layers of their own — the run has four scoring layers, and the panel
shows four rows with L1's three nested under it.

| Layer | Question | Type |
|---|---|---|
| L1a | Is the proposition supported by any authority at all? | Deterministic count |
| L1b | Does this citation exist, and is it the right document? | Deterministic lookup |
| L1c | Is the source trustworthy? (black / gray / white lists) | Deterministic lookup |
| L2 | Does the output actually *use* the cited source? | Retrieval / ranking |
| L3 | Does the output answer the question? | Retrieval / ranking |
| L4 | Is it *faithful* to what the source holds? | Reasoning (Claude Opus) |

Nothing checks whether a quoted passage really appears in the judgment it is hung on. That
check existed and was removed: it turned on a fuzzy 75/90 similarity band, and an honest
paraphrase and an invented sentence land 3.6 points apart under it — noise, and the band sat
25 points above both. See `docs/03-findings.md` Part 3.

Embeddings are used only for retrieval, where they are proven; faithfulness is left to a
reasoning judge, because published results show embedding similarity cannot detect real LLM
hallucinations. See `docs/03-findings.md`.

## Quickstart

```bash
make setup    # uv python install 3.12 && uv sync
make test     # passes offline with NO API keys and no network
make dev      # postgres + redis in Docker, api + worker native
```

The whole stack boots in `PROVIDER_MODE=mock` with an empty `.env`.

## Docs

- `docs/01-architecture.md` — layer DAG, fail-fast gate, aggregation invariant
- `docs/02-contracts.md` — the frozen contract every module compiles against
- `docs/03-findings.md` — live-probe evidence, the threshold literature, and the
  measured calibration against real `voyage-law-2`
- `docs/v1-plan.md` — the approved plan, with what changed during the build
- `todo.md` — open bugs, calibration debt, and deferred scope
