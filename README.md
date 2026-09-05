# SAL Verifier

Automated verification of legal AI outputs. Given a `(question, ai_output)` pair scraped from
claude.ai by a Chrome extension, five layers score it for citation integrity, source trust,
source grounding, responsiveness and factual faithfulness — returning a green check, or a red
check with the reason.

Built for SMU LIT 2026, Problem Statement 2 (Singapore Academy of Law).

## Why it is defensible

**The LLM judge can convict but never acquit.** Layers 1–4 are deterministic and run first; if
any fails, the run fails and the judge is never consulted. The judge only ever sees output that
already passed every machine-checkable test. That invariant is the answer to "who audits the
auditor?", and it is enforced in `pipeline/aggregate.py` and proven by
`tests/pipeline/test_judge_cannot_launder.py`.

## The layers

| Layer | Question | Type |
|---|---|---|
| L1 | Does this citation exist, and is the quote really in it? | Deterministic lookup |
| L2 | Is the source trustworthy? (black / gray / white lists) | Deterministic lookup |
| L3 | Does the output actually *use* the cited source? | Retrieval / ranking |
| L4 | Does the output answer the question? | Retrieval / ranking |
| L5 | Is it *faithful* to what the source holds? | Reasoning (Claude Opus) |

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
