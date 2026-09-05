# TODO

Open work, highest value first. Anything already fixed is recorded at the foot for
context, not as outstanding work.

---

## Bugs to fix

### 1. ~~The contextual prefix fails correct legal work~~ — FIXED

Diagnosed, measured and fixed. See `docs/03-findings.md` F14.

**The cause was the summary, not "the prefix".** Mean pairwise cosine between
Spandeck's own 43 chunks — the quantity ranking inside a document depends on:

| | mean pair | spread |
|---|---|---|
| Raw text | 0.426 | **0.574** |
| Heading path only | 0.435 | 0.565 |
| Summary + heading (shipped) | **0.894** | **0.106** |
| `voyage-context-4` | **0.940** | **0.060** |

The ~1,500-char summary is byte-identical across every chunk, so prefixing it leaves no
two chunks of the judgment more than 0.24 apart. The heading path is short and differs
per chunk, so it disambiguates instead. Over 5 claims: heading costs 2% of mean
similarity and fails nothing; summary costs 23% and fails a correctly grounded claim in
**4 of 4** independent summary draws (0.317–0.349 against the 0.35 floor, versus 0.436
with heading only).

**Two corrections to what this file used to say.** The margin gate was never the one
firing — the prefix suppresses `s_cited` and `s_bg` together and the contrastive design
cancels most of it, so every failure was a *floor* failure. And the intermittency was
not the summary's variability: all four draws fail. It was `split_claims`, which is a
Haiku call whose output varies, so a run may or may not contain the claim that trips.

> **Partly fixed.** The *fragmentation* half is closed: a claim under
> `L3_CLAIM_MIN_CHARS` is restored to the sentence it was cut from, two fragments of one
> sentence collapse to one, and the prompt now says what self-contained means (F18). The
> *non-determinism* this paragraph describes is NOT fixed — `split_claims` still has no
> seed and no cache, so claim counts still vary run to run. Pinning the split, or caching
> it on the answer hash, is the outstanding work.

**Fixed:** `settings.L3_CONTEXTUAL_PREFIX` (`none` / `heading` / `summary_heading`,
default `heading`). The summariser is no longer called on the L3 path unless the regime
uses it, and the regime namespaces the embedding cache so two regimes cannot mix in the
background pool — that mixing would have been a false *green*, so nothing would have
gone red to reveal it.

**`voyage-context-4` is not the alternative.** Now measured: within-document
compression 0.940, decisive paragraph at rank #12–#38. Contextualisation inside the
model does the same damage as contextualisation by string concatenation, because L3
needs to discriminate *within* a document, not between documents. See F15.

**Left open:** the A/B is n=5 claims over one judgment with 22 background chunks from
2 documents. Same caveat class as Part 4 — enough to condemn the summary prefix, not a
benchmark. Re-running it after any change to `EMBEDDINGS_MODEL` is mandatory, and the
harness is not committed yet.

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

### 3. ~~The Chrome extension does not inject~~ — FIXED, confirmed loaded

Verified live on `https://claude.ai`: the panel mounts, captures the prompt and
response, calls the API, polls, and renders all five layers with per-layer status,
score and timing, deterministic findings with source links, the citations list and the
judge section. It verified a real Claude answer end to end against the Docker stack
(29.8 s, cache 43/44, $0.0435).

The three code defects diagnosed earlier were the cause and are fixed: `background.js`
hardcoding `localhost` behind a sticky fallback, `boot()` awaiting `chrome.storage`
before `panel.mount()` in an orphaned content script, and `console.debug` hiding every
diagnostic behind Chrome's Verbose level.

**One operational note worth keeping.** Chrome does not re-read an unpacked extension's
files until the extension is reloaded in `chrome://extensions`; a page reload is not
enough. Editing `panel.css` and refreshing claude.ai shows the OLD stylesheet, which
looks exactly like a CSS change that did not work.

---

### 4. L3 verdicts are not reproducible, because the claim splitter is not

**Severity: high — the same answer verified twice gives different verdicts.**

`chunk_output_claims` calls `Summariser.split_claims`, a Haiku call with no seed and no
cache. The same 2,184-char answer split into 14, 15 and 16 claims across four runs in
one session. L3's status is driven by the **worst** attributed claim, so one extra
claim flips the layer and with it the run.

Measured on one real claude.ai answer verified twice, nothing else changed:

| Run | claims | attributed | L3 | verdict |
|---|---|---|---|---|
| First | 14 | 3 | **pass** 0.545 | warn |
| Replay | 15 | 4 | **fail** 0.279 | fail |

It also silently corrupts any A/B run through the orchestrator: comparing the three
`L3_CONTEXTUAL_PREFIX` arms gave 3/14, 4/16 and 4/16 claims — three different claim
sets — and a clean-looking table that measured the splitter, not the prefix. F16's
numbers are only meaningful because the split is pinned across arms.

Pre-existing, not introduced by the prefix change, but it was masked while the prefix
was failing things outright, and it is why F14's live failure looked intermittent.

**Next step, cheapest first:**
- Cache the split on `sha256(ai_output) + model + prompt_version`, exactly as
  `document_summaries` caches the document summary. A re-verification of the same
  answer then cannot drift, which is most of the problem.
- Set `temperature=0` on the split call if the provider exposes it.
- Report `claim_strategy` and the claim count in the panel, so a verdict that rests on
  4 claims rather than 3 is visible rather than inferred.
- Longer term, the deterministic `window_claims` fallback is reproducible by
  construction. It is worth measuring whether it is good enough to be the default.

Files: `src/verifier/semantic/chunking.py`, `src/verifier/providers/*_llm.py`,
`src/verifier/repos/documents.py` (the summary cache is the pattern to copy).

---

### 5. The Haiku citation extractor has not been measured, and is not cached

**Severity: high — L1a now has no deterministic floor under it.**

L1a used to count regex matches. It now counts what Haiku returns, and there is no
union with the regex: if the model returns a well-formed but *short* list on an answer
that cites one thing, L1a reports "cites nothing" and fails the run. That is the same
false red the change was built to remove, arriving by a new route, and nothing in the
test suite can catch it — every test supplies its own candidates.

Two guards exist and neither closes this. A candidate must appear **verbatim** in the
answer, which stops invention but not omission. A degraded extractor never fails, which
stops an outage being read as an uncited answer but says nothing about a bad list.

**What to do, in order:**

1. **Measure recall.** Run five or six real claude.ai answers through
   `extract_with_llm` and diff the model's citation list against `extract()`'s on each.
   Anything the regex found and the model missed is the bug. This is the evidence that
   decides whether dropping the regex floor was right; until it exists, the decision is
   a design judgement, not a measured one, and is deliberately absent from
   `docs/03-findings.md` for that reason.
2. **Cache the call**, keyed `sha256(ai_output) + model + EXTRACTOR_PROMPT_VERSION`.
   This is the same defect as bug 4 above and wants the same fix — `document_summaries`
   is the pattern, and one cache should serve both the claim splitter and the extractor.
   L0's output feeds *every* layer and is persisted (`finding.citation_ordinal`), so
   drift here is worse than drift in the splitter. `temperature=0` is pinned on the call
   already, which narrows it but does not close it.
3. **Consider a floor.** If (1) shows misses, the cheapest fix is to union the regex
   citations back in for the FAIL count only — the model can then add authority but
   never remove it. That was the original design and was dropped for simplicity; the
   measurement should decide.

### 6. Two different cases merge into one cluster when a report citation follows a neutral one

**Severity: medium — the second case is never resolved, so it is never checked.**

`_would_conflict` (`extraction/citations.py:213-228`) allows unlimited REPORT members in
a cluster, on the reasoning that parallel report series for a single case are common and
none of them resolves anyway. That is true of `[2018] 1 SLR 1` sitting beside
`[2018] SGCA 41`. It is not true of a *different case* cited within the 80-character
window.

Seen on a live Haiku run over an answer citing NTUC Foodfare and then Caparo:

```
cluster 2: [('case_name', 'NTUC Foodfare ... v SIA Engineering Co Ltd'),
            ('neutral',   '[2018] SGCA 41'),
            ('report',    '[1990] 2 AC 605')]     <- this is Caparo
```

Caparo is now a "parallel citation" of NTUC. `_resolve_all` keys on
`cluster.preferred.citation_key`, which is the neutral one, so Caparo is never looked up
and never checked. It still counts as authority for L1a, so it produces no visible
error — the citation simply goes unverified while the panel shows the run as covered.

Pre-existing, not introduced by the LLM extractor, but the extractor makes it easier to
hit: a model finds citations the regex missed, so more of them land in the window.

**Fix:** a report citation should only join a cluster that has no *intervening* citation
of another case, or more simply, a cluster should not absorb a REPORT that is separated
from its neutral sibling by a case name. Worth a test in
`tests/extraction/test_citations.py` either way.

### 7. The cached HTTP client outlives the event loop, so every other run loses its first fetch

**Severity: high — citations are silently not checked, and it looks like a source outage.**

Reproduced deterministically inside the api container:

```
task 1: 200 848b
task 2: RuntimeError: Event loop is closed
task 3: 200 848b
```

Three pieces, each reasonable alone:

* `get_http_fetcher()` is `@lru_cache`d (`providers/factory.py:17`), so there is one
  `HttpFetcher` per process.
* `HttpFetcher._ensure_client()` (`providers/fetcher_http.py:126-138`) builds one
  `httpx.AsyncClient` on first use and keeps it on the instance.
* `worker/tasks.py:88` runs each Celery task with `asyncio.run(coro)`, which creates a
  fresh event loop **and closes it** when the task ends. The forked worker process is
  then reused for the next task.

So the connection pool holds keep-alive connections bound to a loop that no longer
exists. The next task's first fetch picks one up and raises; the pool then evicts it and
the task after that succeeds — which is why it presents as intermittent rather than as a
hard failure, and why it is invisible in the test suite (nothing there crosses two
`asyncio.run` calls with a live client).

**What it costs.** `client.py:179-187` catches any transport failure and returns
`ResolutionStatus.ERROR` with `detail="fetch_failed:RuntimeError"`, which L1b reports as
`CITATION_UNVERIFIED` — "could not be checked". That is the safe direction, so it never
manufactures a fabrication. But the citation is simply not verified, and the panel says
"the lookup failed", which is exactly what a genuine source outage says. Observed live:
a browser run against `[2013] SGCA 39` reported the lookup as failed while eLitigation
was answering other requests in the same run.

**Fix:** build the client per event loop rather than per process — cheapest is to drop
the instance-level cache and open an `AsyncClient` per `fetch` (or per run), or key the
cached client on `asyncio.get_running_loop()`. Worth a test that calls `asyncio.run`
twice against one cached fetcher, since that is the shape no existing test has.

### 8. `docker-compose.override.yml` reports real cases as fabricated

**Severity: high — the demo config produces the exact failure the product exists to prevent.**

The override exists to survive an eLitigation maintenance window, and stubs the fetcher
by flipping `PROVIDER_MODE: mock` while keeping embeddings, summariser and judge real.
But the mock fetcher serves `tests/corpus`, which contains **two real judgments**:

```
2007_SGCA_37.html  2021_SGHC_100.html  maintenance_notice.html  soft404_2019_SGCA_999.html
```

Every other citation resolves as a soft-404, which is `NOT_FOUND`, which is the one
citation-level **FAIL**. Observed live on a real claude.ai answer: `[2013] SGCA 29`,
`[2021] SGCA 28` and `[2020] SGHC 32` — all real Court of Appeal and High Court
decisions — were reported to the user as *"3 fabricated citations … The source was
reachable and reported no such judgment. This is positive evidence the authority does
not exist."*

The same answer re-verified with the real fetcher, during the same maintenance window,
returns four `CITATION_UNVERIFIED` warnings and a deterministic **WARN**. The maintenance
handling (F12) works; the demo override defeats it.

**Fix:** the override should not be the default `docker compose up` configuration. Either
rename it so it is opt-in (`-f`), or have `MockFetcher` return `UNRESOLVABLE` rather than
a soft-404 for anything outside the corpus — a stub that does not hold a document has not
established that the document does not exist.

### 9. The L3 floor may be wrong for negative and meta claims

**Severity: medium — one confirmed instance, not yet a pattern.**

All three prefix arms fail this claim from a real Claude answer:

> *"The court expressly declined to treat pure economic loss as attracting a separate or
> more restrictive control device ..."*

Spandeck [115] says close to the opposite, so it may be a true positive. But L3 asks a
**retrieval** question, and a claim about what a court *declined* to do is a negative
proposition that matches no single paragraph well even when accurate — the asymmetry
arXiv:2504.16318 names, and the reason faithfulness belongs to L5. Here L3 short-
circuited the judge, so the layer that could actually rule on it never ran.

The floor is calibrated on `n=5` positive assertions. Whether it is the right instrument
for meta-claims is untested.

**Do not fix by lowering the floor** — that is the tuning-around-a-bug Part 4 forbids.
Widen the calibration set with negative and meta claims first and find out whether they
form a separable regime.

---

### 10. A maintenance-window resolution is cached permanently

**Severity: high — one outage poisons the cache for every later run.**

Found during the first real-stack run. eLitigation was in a maintenance window (F12,
third occurrence). The neutral-citation URL fetch correctly detected it, the search
fallback then matched the case name against the maintenance page, and the run stored:

```
citation_key   | status   | method | confidence | expires_at
sgca:2007:37   | resolved | search | 1.0        | NULL
```

`documents` stayed empty, so the row says **resolved** while no text exists. Every
later run reuses it and reports `CITATION_UNVERIFIED — exists, but its text was not
available`, and it will keep doing so after eLitigation comes back, because
`expires_at` is NULL and nothing invalidates it.

The verdict direction is safe (WARN, never FAIL — the F12 rule holds). The defect is
that a *transient* source state was written to a *durable* cache with no expiry, and
the only way out today is deleting the row by hand.

**Next step:** do not cache a resolution whose document has no usable text, or give
SOURCE_UNAVAILABLE resolutions a short `expires_at`. The column already exists and is
never populated.

Files: `src/verifier/repos/resolutions.py`, `src/verifier/pipeline/resolver.py`.

---

### 11. ~~The Docker image does not ship `tests/corpus`~~ — FIXED

**Was: medium — the compose file's own promise was untrue.**

`docker-compose.yml` opens with "The whole stack boots with an empty .env in
PROVIDER_MODE=mock." It boots, but it cannot verify anything: `MockFetcher` reads
`tests/corpus/*.html` via `parents[4] / "tests" / "corpus"`, and the image copies only
`src`, `alembic`, `pyproject.toml` and `README.md`. Every citation resolves to nothing.

Verified by running the acceptance pair inside the `api` container: both returned
`CITATION_UNVERIFIED`. The same script run natively, against the same Postgres, passed.

**Fixed** by `COPY tests/corpus ./tests/corpus` in `docker/Dockerfile`, alongside the
browser work. ~240 kB, and it is the difference between mock mode working and mock mode
lying: without it every citation in a container soft-404s, and a soft-404 is `NOT_FOUND`,
which is the only citation-level FAIL — so the offline demo reported real cases as
fabricated. Note this makes bug 8 sharper rather than softer: the stubbed fetcher now
genuinely resolves the two judgments the corpus holds and still fails every other real
case, which looks more convincing and is just as wrong.

Files: `docker/Dockerfile`.

---

### 12. There is no `FETCHER_MODE`

**Severity: low — but it is what made bug 10 hard to work around.**

`EMBEDDINGS_MODE`, `SUMMARISER_MODE`, `JUDGE_MODE` and `EXTRACTOR_MODE` can each be set
independently of `PROVIDER_MODE`. The fetcher cannot: `get_http_fetcher()` keys on
`settings.is_mock`, which is the global switch. So "real models, local corpus" — the exact configuration
used for every calibration run in `docs/03-findings.md` Parts 4 and 5, and the only way
to exercise L3/L5 while eLitigation is down — requires flipping the global to `mock`
and setting the other three back to `real`.

Files: `src/verifier/providers/factory.py`, `src/verifier/settings.py`.

---

### 13. `make seed-lists` reports success while writing nowhere durable

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

### 14. A browser fetch runs inline, in front of the fabrication check

**Severity: high — it is a timeout away from taking a whole run down.**

`worker/celery_app.py` says why `QUEUE_BROWSER` exists:

> `browser` -- login-walled fetches. Heavyweight and long-lived; isolated so a hung
> browser session cannot take the fast path down with it.

Nothing dispatches to it. The whole pipeline runs inside `run_verification` on
`QUEUE_DEFAULT` via `asyncio.gather`, so an SSO fetch — which launches Chromium, waits
out an AWS WAF challenge, and renders a 900kB page — sits directly in front of the ~0.6s
deterministic fabrication check that is the product's fastest and most valuable signal.

The numbers do not leave much room: `BROWSER_TIMEOUT_S` is 45.0 against a `RUN_SOFT_LIMIT`
of 45, plus a multi-second `_ensure_context` cold start, and `SOURCE_TIMEOUT_S` is
httpx-only and does not bound the browser path at all.

`SOURCE_BROWSER_INLINE_TIMEOUT_S` (default 20s) is the mitigation and is deliberately not
called a fix: it bounds the damage, it does not restore the isolation. The fix is to
dispatch browser fetches to `QUEUE_BROWSER` — which means the resolver awaiting a Celery
result mid-pipeline, i.e. exactly the nested-coordination shape `orchestrator.py`'s
docstring rejects ("the failure mode is a run that silently never finishes"). That
tension is the real work, and it is why this was not done under time pressure.

Files: `src/verifier/pipeline/orchestrator.py`, `src/verifier/worker/celery_app.py`,
`src/verifier/sources/sso/client.py`.

---

### 15. `browserworker` is unreachable

**Severity: low — it is dead weight, not a wrong answer.**

Direct consequence of bug 14. The profile-gated `browserworker` service starts, connects
to Redis, subscribes to `QUEUE_BROWSER` and waits forever, because nothing ever puts
anything there.

No source currently needs it, which is the only reason this is low severity. Both
implemented adapters declare `FetchStrategy.HTTP` — SSO included, because a headless
browser is the one client its WAF blocks outright (bug 16 / F21). `api` and `worker` were
briefly built with `INSTALL_BROWSER=true` for the SSO-over-browser design and are back on
the slim image now that the measurement killed it.

So the browser path is wired, tested and reachable, and nothing exercises it end to end.
The first login-walled source — LawNet — makes both this and bug 14 live at once.

Files: `docker-compose.yml`, `docker/Dockerfile`.

---

### 16. ~~SSO's soft-404 has not been measured~~ — FIXED, measured

Measured through the adapter's own fetcher, three states, all captured in `tests/corpus`:

| Page | Status | Bytes | `<title>` |
|---|---|---|---|
| `Act/IA1959` | 200 | 345,880 | `Immigration Act 1959 - Singapore Statutes Online` |
| `Act/ZZZ9999` | 200 | 24,693 | `Page Not Found - Singapore Statutes Online` |
| WAF refusal | 403 | 919 | `ERROR: The request could not be satisfied` |

The title carries it and length only corroborates — F12's finding, reached independently
for a different site. `PageState` now has all three members and `NOT_FOUND` is wired.

It also corrected the fetch strategy. The adapter first declared `BROWSER`, reasoning that
a `202 x-amzn-waf-action: challenge` meant "browsers only". Both halves were wrong: the
challenge was RATE-based and cleared on its own, and a headless browser is the client SSO
blocks hardest (403), so the browser path was strictly worse than httpx. What the WAF
wants is a conventionally-shaped UA and a polite rate, both of which we give it honestly
via `SSO_USER_AGENT`. Nothing is done about the headless block: a site refusing automated
browsers is a preference to respect, and the HTTP path needs no workaround anyway.

---

### 17. SSO does not serve an Act's text, so no statute can be quote-checked

**Severity: high — it is the ceiling on what L1c and L3 can ever say about legislation.**

`Act/IA1959` is 346kB containing a 106-entry table of contents and **four** provisions —
1,968 characters of statutory text. The body is fetched by the page's own JavaScript (a
Knockout app; the control is `data-bind="click: OnGetProvisions"`), and `?WholeDoc=1`,
which the site's own "Whole Document" button navigates to, returns the identical four.

The consequence if this were ignored is the worst failure this system has: a document
built from that HTML holds sections 1–4, so quote-checking section 57 would score near
zero and emit `QUOTE_NOT_FOUND`, which is a FAIL — **a real statute, correctly quoted,
reported as fabricated.**

What prevents it today is that `SsoAdapter.document_for` returns `None`, so L1c's
"no document" branch stays silent and L3 returns NOT_APPLICABLE. That is the conservative
direction and it holds, but it is a floor, not a fix: an SSO citation can be confirmed to
EXIST and can never be checked for what it SAYS.

Two ways out, neither in scope yet: a browser that runs the page's JS (SSO answers
headless Chromium with 403, so this needs a headed or stealth browser — the first is
impractical for a server, the second is evasion and is out of the question), or the
provisions endpoint the page's own JS calls, used under the same honest UA and rate limit.
The second is the one worth investigating.

Files: `src/verifier/sources/sso/client.py`, `src/verifier/sources/sso/parser.py`.

---

## ~~Pending: the live end-to-end run against eLitigation~~ — RUN, 2026-09-05

**Run and written up as `docs/03-findings.md` Part 8 (F23–F29).** eLitigation was back
(`200`, 153,370 bytes) and the stack was all-real and unmodified — the running image was
verified byte-identical to `HEAD` before the run, and the database was empty at the start.

**The thing under test passes.** Four Singapore neutral citations resolved through
`sources/registry.py` to real judgments, every row carrying `domain = www.elitigation.sg`
and `fetch_strategy = http` (F23). Three of them are the same `[2013] SGCA 29`,
`[2021] SGCA 28` and `[2020] SGHC 32` that bug 8 saw reported as *fabricated* under the
mock override — against the live source they resolve, with documents, which closes bug
8's diagnosis. A correct Singapore answer verifies **pass** end to end, L1 1.0 / L3 0.537
/ L5 1.0, for `$0.0338` (F24). L3's top passage is Spandeck [72] with a paragraph range
and a live URL, so F13's fix works on real data.

**It did not work through the deployed worker, and now does.** The run took 53.4 s
against a `RUN_SOFT_LIMIT` of 45, so all 4 API-dispatched runs were killed and the
clean `pass` had to be obtained in-process, outside Celery. Bugs **18, 19 and 20 are
now fixed** (`2607f5a`) and re-verified live through the worker — `docs/03-findings.md`
Part 9: a cold run completes in 79 s, a warm one in **26.5 s** on `170/1` cache hits,
and the judge runs on its own queue. Bugs 21–24 below remain open.

Two runbook side-quests are **not** done and are still worth doing: bug 5's recall
measurement for the Haiku extractor, and a deliberate re-check of bug 7 (no
`fetch_failed:RuntimeError` was observed, but `sgca:2007:37` did fall back to `search`
where the direct URL had worked moments earlier, which is that bug's fingerprint).

---

### 18. ~~Nothing reads the durable embedding cache, so no run is ever warm~~ — FIXED

**Fixed in `2607f5a`, verified live: `docs/03-findings.md` F30.** `text_embeddings` went
0 -> 170 rows, a second run hit `170/1` where every run before it reported `0/171`, and
the deterministic phase dropped **46.0 s -> 11.5 s**. The L3 score is unchanged to three
decimals (0.548 cold and warm), which is the property that matters: a cache that moved
the score would be a bug wearing a speedup's clothes.

The cause was as diagnosed below. `default_embedding_repo()` now takes the repo from
`get_repos()`, and `CachedEmbedder`'s three repo calls are best-effort, because the repo
is now network I/O rather than a dict that cannot fail.

<details><summary>Original diagnosis</summary>

After a **fully successful** run: `chunks` 0, `text_embeddings` 0, `document_summaries`
0, `cache hits 0 / misses 171`. `PgEmbeddingRepo` is implemented and constructed by
`build_pg_repos()`, and **nothing ever reads it**. `layers/registry.py:build_layer`
constructs `SourceGroundingLayer()` and `ResponsivenessLayer()` with no arguments, so
`embedding_repo` falls through to `semantic/defaults.py:default_embedding_repo()` —
an `InMemoryEmbeddingRepo`. `embedding_repo` is never passed anywhere in the tree.

So every run re-embeds the whole judgment (~40 s), which is precisely what pushes a
53.4 s run past a 45 s budget: **the cache that would fix the timeout is the broken
thing.** It also means L3's contrastive background pool is whatever one process has
seen rather than the corpus, so Part 4's margin is calibrated against a pool that does
not survive a restart.

Same shape as the already-fixed "Documents never written to durable storage". That fix
landed for `documents` and not for embeddings — documents persisting is what made this
visible.

Files: `src/verifier/layers/registry.py`, `src/verifier/semantic/defaults.py`.

</details>

---

### 19. ~~A killed task leaves the run `pending` forever~~ — FIXED

**Fixed in `2607f5a`, verified live against real Postgres: F32.** `pending` ->
`status=error, is_final=True`, idempotent under redelivery, and `GET /v1/runs/{id}`
reports it as terminal so the extension stops polling.

It had **two** independent leaks, not one. `SoftTimeLimitExceeded` subclasses
`Exception`, so it lands wherever the signal interrupts: usually the event loop's
`select`, escaping `asyncio.run` (fixed by the new handler); sometimes inside the
orchestrator's own `except`, which set `status=ERROR` but never `is_final` (fixed
separately at `orchestrator.py:293-303`). Fixing either alone still hung.

<details><summary>Original diagnosis</summary>

There is no `except SoftTimeLimitExceeded` in `worker/tasks.py`. When the limit fires
Celery marks the *task* `FAILURE`; the *run row* keeps its last status and `is_final`
stays `false`, so `GET /v1/runs/{id}` says `pending` indefinitely. Three of four runs
are still `pending` in the table.

The panel's own timeout is what conceals it — the user sees "The verification timed
out" while the API still claims the run is in flight.

**Fix:** catch `SoftTimeLimitExceeded` and write a terminal `error` state. The soft
limit exists to give exactly this chance to clean up; nothing uses it.

Files: `src/verifier/worker/tasks.py`.

</details>

---

### 20. ~~The judge is never deferred, so `judgeworker` is dead weight~~ — FIXED

**Fixed in `2607f5a`, verified live: F31.** `judgeworker` received and succeeded its
first task ever (16.5 s, `judge_ran: True`), and both runs passed through
`deterministic_ready` before `complete` — the deterministic verdict rendering while the
judge is still cold, which the two-phase design was built for and had never exhibited.

`_dispatch` now passes `defer_judge=True`. A failed handoff falls back to the inline
judge, because deferral would otherwise introduce a new way to hang: an unreachable
broker would park the run at `deterministic_ready` with nothing left to finalise it.

<details><summary>Original diagnosis</summary>

`api/deps.py:_dispatch` calls `task.delay(run_id)` with no `defer_judge`, which defaults
to `False`. The branch at `tasks.py:134` that sends the judge to `QUEUE_JUDGE` is
therefore unreachable from the API, and a judge budgeted `JUDGE_SOFT_LIMIT = 90` on its
own queue runs inline inside the deterministic task's 45. Confirmed live: `judgeworker`
subscribes to `judge`, reports ready, receives nothing, and `llen judge` is 0 mid-run.

Dispatching the judge by hand finalised a stalled run in one poll — the deferred path
works and is simply never taken. This is bug 15 at the judge queue instead of the
browser queue.

**Fix:** pass `defer_judge=True` from `_dispatch`. That alone takes ~7–13 s of judge off
the deterministic budget and puts it where the design already says it belongs.

Files: `src/verifier/api/deps.py`.

</details>

---

### 25. ~~The panel gives up at 45 s, so a healthy cold run reads as a timeout~~ — FIXED

**Found by running it.** Raising `RUN_SOFT_LIMIT_S` to 150 (bug 18/19/20 work) moved the
false timeout from the worker to the panel rather than removing it:
`extension/src/config.js` still had `pollTimeoutMs: 45000`, matching the *old* server
budget. A cold run takes ~79 s and completes, and the panel abandoned it at 45 s and
rendered "The verification timed out" over a verdict already on its way.

Now 240000, derived from the server's own budget rather than picked: `RUN_SOFT_LIMIT_S`
(150) for the deterministic phase plus `JUDGE_SOFT_LIMIT_S` (90) for the judge on its own
queue is the longest a run can legitimately take. The stale comment claiming "a run is
<=20 s" went with it.

**Necessary but not sufficient.** Changing the default did not change the behaviour,
because `chrome.storage` held a stale copy that overrode it on every boot — bug 26.
Both fixes are needed, and the extension must be reloaded in `chrome://extensions`
(Chrome does not re-read an unpacked extension's files on a page refresh).

The general lesson is worth keeping: a client timeout and a server timeout are one
setting in two files, and changing either alone just relocates the failure.

---

### 26. ~~`saveConfig` persisted the whole config, freezing every tuning constant~~ — FIXED

**Severity: high — it silently defeated every later change to a default, in a place no
file shows.**

`saveConfig` is only ever called with `{panelView}` or `{panelTheme}`, but it stored
`{...SALV.config}` — the **entire** object — and `loadConfig` did
`Object.assign(SALV.config, stored.config)`, applying that blob over the defaults.

So the first time anyone clicked the full-screen or theme toggle, the extension wrote a
complete snapshot of that session's config into `chrome.storage.sync`, `pollTimeoutMs:
45000` included. From then on the shipped defaults were dead on arrival. That is why
bug 25's fix appeared not to work: the default was raised to 240000, then to 180000, the
extension was reloaded, and the panel still gave up at exactly 45 s — the stale value was
not in any file, so nothing in the repo showed it, and reloading could not clear it.

**Fixed** with a `PERSISTED_KEYS = ['panelView', 'panelTheme']` allowlist applied on
**read as well as write**. Filtering the read is the half that matters for anyone who
already has the bad blob: it heals an existing profile without asking them to clear site
data. Verified against a simulated poisoned profile — the file's default wins, both real
preferences survive, and a save now writes only those two keys.

Same family as bug 3's `background.js` sticky `localhost` fallback: a value cached
somewhere invisible, outliving the code that set it.

---

### 21. The extension verifies the sidebar, and calls it a FAIL

**Severity: high — this is a false red on text the user never asked about.**

Driving the panel on live claude.ai produced **14** runs of `complete | fail` whose
`ai_output` is a sidebar conversation title and its date:

```
"Probability problem answer verification  Aug 22"
"Continent selection requirement  Aug 24"
"Runtime error in two-sum cement bag solution  Aug 26"
```

The structural tier finds the recents list's repeated group and calls it an assistant
turn. This file already records the symptom under QoL as cosmetic and limited to `/new`.
It is neither: it fires on `/recents`, and a run over a 40-character title finds no
citations and no grounding, which resolves to a hard **FAIL** — the worst verdict the
system emits. The real answer was captured correctly in the same session, so the capture
path is fine; the ladder just does not reject non-answers before spending a run.

**Fix:** a minimum-plausibility gate on the captured turn before dispatch (length, and
the absence of the sidebar's date suffix shape), and never a FAIL from a run that found
nothing to check.

**Extension half done; STILL OPEN on the backend.** The sidebar no longer reaches a run
at all: `classifyByClassFrequency` refuses a split it cannot evidence (a class token
balanced across 25-75% of siblings, or one side's median at least 2x the other's), and
navigation is excluded from the group search by text-share rather than by class name.
A recents list fails both tests -- identical classes, identical lengths -- so tier 3
returns null and the ladder falls through to the manual trigger. `buildRequest` also
refuses anything under 60 characters unless the user pointed at it directly. Measured on
`extension/dev/selector-fixture.html?shape=sidebar`: 14 rows in, 0 messages out, tier
`manual`.

The date-suffix half of the fix above was deliberately NOT implemented: it encodes one
locale and one date format and stops matching the day claude.ai renders "2 days ago".
The length floor covers every logged case without that assumption.

What remains is the second half, and it is a backend change: `deterministic_verdict`
(`src/verifier/pipeline/aggregate.py`) still returns FAIL on any FAIL-severity finding
however little was actually checked, and the panel relabels only the *pill* for citation
coverage, never the verdict. A run that checked nothing can still emit the worst verdict
in the system -- it just can no longer be reached from the sidebar.

---

### 22. A truncated embeddings response takes L3 down with no retry

**Severity: medium — safe direction, but the layer is lost and the run still pays.**

```
L3 could not complete: Response payload is not completed:
<ContentLengthError: 400, 'Not enough data to satisfy content length header
(received 27699 of 707298 bytes)'>
```

A Voyage response was cut off at 4% of its declared length. One truncated batch errors
the whole layer, because nothing retries. The verdict degraded to `warn` with a
`LAYER_ERROR` finding rather than failing anything — the F12 rule holding — but L3
contributed nothing and the run still bought a judge call.

**Fix:** retry a truncated/short read, the same way any transport error should be
retried. Worth pairing with bug 18, since a warm cache makes the batch smaller.

---

### 23. `citation_resolutions.document_id` can end up NULL for a resolved row

**Severity: medium — bug 10's shape, reached by a different route.**

`sgca:2007:37` was written by one run as `method=url` with a document, then rewritten by
a later run as `method=search` with `document_id` NULL while the document row still
existed and was used by that same run. A `resolved` row pointing at no document is
exactly bug 10's poisoned state, and `expires_at` is NULL, so it is permanent.

Bug 10 remains open and this is a second way in: it is not only maintenance windows that
write a durable row for a transient state.

Files: `src/verifier/repos/resolutions.py`, `src/verifier/pipeline/resolver.py`.

---

### 24. Embeddings never carry a `document_id`, so L3's background pool is always empty

**Severity: high — the contrastive margin has never run in production.**

Newly visible now that the cache is durable (F33). All 170 cached vectors have
`document_id = NULL`:

```
embeddings with doc: 0 of 170
```

`_document_key` (`layers/l3_alignment.py:645-656`) falls back to the document's **content
hash** when `SourceDocument.id` is unset, which it is for a freshly-fetched document.
`PgEmbeddingRepo.put_many` then does `uuid.UUID(document_id)`, gets `ValueError`, and
stores `NULL`. `sample_background` filters `document_id IS NOT NULL` and matches nothing.

L3 handles it correctly and reports it — `background_empty: true`, `margin_skipped: true`,
with a note saying only the absolute floor was applied — so this is safe degradation, not
a silent failure.

**Not a regression.** The in-memory repo started cold in every task and had only ever
seen the document under test, so the pool was empty then too. What changed is that it is
now *permanently* empty rather than incidentally so: 170 durable vectors sit in the table
and none can be sampled.

The consequence is worth stating plainly: **every live L3 verdict to date rests on the
absolute floor alone.** Scores are unaffected (they are `max cos(claim, cited)`, which
does not involve the margin), but the margin half of Part 4's calibration has never been
exercised outside the offline set.

**Fix:** carry the persisted row id onto the `SourceDocument` before chunking — the
orchestrator already learns it at `orchestrator.py:610` (`stored.id`) and writes it back
onto the resolution, so the id exists and simply never reaches L3. Failing that,
`PgEmbeddingRepo` could store a deterministic UUID5 of the content hash rather than
discarding a non-UUID key. The first is the real fix.

Files: `src/verifier/layers/l3_alignment.py`, `src/verifier/repos/embeddings.py`,
`src/verifier/pipeline/orchestrator.py`.

---

## QoL improvements

### ~~The panel is unreadable in dark mode~~ — FIXED

The dark-mode block overrode surfaces and left the **ink** at its light-mode values.
`.salv-finding-msg` stayed `#23262e` on a `#1f222a` card — a contrast ratio of
**1.05:1**, i.e. invisible — while the pills and links were overridden and looked
correct, so the panel read as working.

Measured, before and after, in dark mode:

| | before | after |
|---|---|---|
| Finding body on its card | **1.05** | **12.37** |
| Unresolved citation | 1.79 | 7.89 |
| Code / meta labels | 4.14 | 6.21 |
| Worst pair anywhere, either theme | — | **5.23** (AA is 4.5) |

Fixed by making every colour a custom property and having the dark block redefine
**only tokens** — no selector in it styles anything, so light and dark cannot drift
apart again. Semantic inks are lightened rather than reused: `#8f1d1d` on a dark card
is 1.79:1 however correct the hue.

Type was also raised for the actual readers — lawyers reading dense findings with
paragraph pinpoints, not developers skimming a debug overlay. Body 13→15 px, finding
text 12→14 px, evidence 11→13 px, meta 10→12 px, all driven by a single
`--salv-scale` multiplier so nothing drifts out of proportion.

**Full-screen reading view** added: `⤢` in the header expands the 380 px rail to a
centred, max-1100 px sheet and raises the same `--salv-scale` to 1.15. The choice is
remembered in `chrome.storage` — written best-effort and applied only *after* mount,
because the panel's existence must never depend on a storage round trip (that was the
orphaned-content-script hang in bug 3).

---

### Smaller things spotted in the same pass

- ~~**The panel shows an error on an empty chat.**~~ Fixed at the root, and it was never
  cosmetic — it was bug 21 caught one step earlier. On `/new` the structural tier found
  the sidebar's repeated group and called half of it an assistant turn; the *first* such
  row had no preceding "user" row so it threw and painted the Error pill, and **every
  other row dispatched a real run**. That asymmetry is why the selector was fixed first:
  routing the throw to idle on its own would have removed the pill and kept the
  dispatches, turning a loud bug into a silent one.

  Two changes, either of which now suffices. The ladder no longer invents a transcript
  out of navigation chrome (bug 21 above). And "nothing to verify" is no longer an
  error: `buildRequest`'s benign throws are tagged, and an *automatic* scan that finds
  nothing renders nothing at all — the panel is already idle from boot, and painting
  over it would let one misclassified node wipe a real answer's verdict off the screen.
  Only an explicit toolbar or right-click gesture gets an explanation back. The Error
  pill is now reserved for a broken tool: backend unreachable, contact lost, run
  forgotten, timeout.

- ~~**Pending layers reported as `Skipped`.**~~ Mid-run `state.layers` is empty, and
  `layerRow` fell back to `'skipped'` for any layer without a result — so all five rows
  read `Skipped` while the run was still in flight. `skipped` is a *terminal*
  `LayerStatus` meaning the layer was deliberately not run, which is what fail-fast does
  to L5; using it for *not started yet* told the reader five layers had looked and
  declined at a moment when none had reported. It is the same objection `statusLabel`
  already makes to softening `not_applicable` into "skipped", in the other direction.
  A pending row now carries the run's elapsed time instead, measured client-side from
  dispatch (the backend's `timings` only fill in as the run completes) and ticking off
  pollRun's existing 400 ms re-render with no timer. `render` also stops claiming "No
  deterministic problems found" before anything has been checked.

- ~~**A stale verdict survived navigation.**~~ `renderIdle` was called exactly once, at
  boot, and there was no history listener anywhere — so SPA-navigating from a checked
  answer to a new chat left the previous conversation's verdict on screen. The debounced
  body observer now compares `location.pathname` and resets on change, above the
  `autoVerify` guard so it still fires for someone who has automatic verification off.
  `popstate` is wired too but is only for back/forward: it does not fire on
  `history.pushState`, which is what "New chat" actually does.

  Three latent races had to go with it, or the reset half-works intermittently, which is
  worse than not fixing it. `pollRun` only checked `cancelled` at the top of its loop, so
  an in-flight poll resolving after navigation repainted the old verdict ~400 ms later;
  `watchForCompletion` never checked `isConnected`, so a node mid-settle at navigation
  verified against a conversation that had left the screen; and `verifyNode` cancelled
  the active run *before* knowing the node was verifiable at all — which, combined with
  the dedupe early-return, left `active` permanently non-null after `pollRun` exited
  without clearing it.
- ~~**Long evidence passages are not scrollable.**~~ Fixed in the serif/light restyle:
  `.salv-evidence` is capped at `calc(var(--salv-scale) * 190px)` with `overflow-y:
  auto`, so a long retrieved passage no longer pushes the layer table below the fold.

## Calibration debt

- **Widen the L3 calibration set.** `make l3` scores 3 genuine and 4 foreign claims
  against one judgment. Enough to rank configurations — it caught a change that improved
  every score while narrowing the gap (F19) — and nowhere near enough to state a
  threshold. Every genuine claim is from Spandeck; no second judgment, no second area of
  law.
- **Measure the false-PASS side deliberately.** The foreign claims are from other areas
  of law. The harder negative is a claim about the *same* area that the cited judgment
  happens not to decide, and nothing here covers that.

- **Widen the threshold samples.** L4 is calibrated on `n=11`, L3 on `n=5`. Enough to
  replace a demonstrably wrong threshold with a measured one; not enough to quote a
  confidence interval. See `docs/03-findings.md` Part 4.
- **L1's quote bands are still uncalibrated under `partial_ratio`** at scale — the
  75/90 figures separate the regimes cleanly on one judgment, which is not the same as
  being right across the corpus.
- **Every number is model-specific.** Changing `EMBEDDINGS_MODEL` invalidates all of
  them — and so does changing `L3_CONTEXTUAL_PREFIX`, which decides what text is
  embedded before any threshold sees it. The current figures are calibrated against
  `heading`.

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
| The claim splitter cutting a sentence into a fragment that says the second stage of nothing — 0.313 against 0.649 for the sentence itself (F18) | restoring foreign claims to the calibration set |
| Self-contained claims failing to locate, so half of them were never attributed and never scored | measuring the fix for the line above |
| Two findings both numbered F13 | writing up F18 |
| The judge given one passage per claim, truncated at a byte offset, labelled with the wrong paragraph | first full e2e |
| The harvest cap applied to arrival order, so L1 evidence displaced L3's ranking | reading the orchestrator's layer order |
| `background.js` still on `localhost` after the 127.0.0.1 fix — on the sticky proxy path | grepping the extension |
| `boot()` awaiting a `chrome.storage` read that never settles, before mounting the panel | tracing the injection symptom |
| Selector tier 1 classifying three action-bar buttons as user messages, and winning the ladder with no assistant in the result | driving the extension on live claude.ai |
| `dedupeNesting` keeping the outermost match, so the `aria-label="Chat messages"` wrapper displaced both real turns | same |
