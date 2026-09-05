"""Centralised typed config.

Every vendor key defaults to blank so the whole stack boots with no secrets in
PROVIDER_MODE=mock. Production fails loudly instead of degrading silently.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=True
    )

    # --- Runtime ---
    ENV: Literal["development", "production", "test"] = "development"
    ROLE: Literal["api", "worker", "judgeworker", "browserworker", "beat", "migrate"] = "api"
    PROVIDER_MODE: Literal["mock", "real"] = "mock"
    LOG_LEVEL: str = "info"

    # --- API ---
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:3000,chrome-extension://*"

    # --- Infrastructure ---
    #: Storage backend, kept SEPARATE from PROVIDER_MODE on purpose.
    #:
    #: Vendor selection and storage selection are different concerns, and coupling
    #: them made the Postgres path unreachable without paid API keys: `make dev`
    #: would start a database that the API then ignored, so the repos went
    #: unexercised until production. "auto" preserves the old behaviour (memory in
    #: mock mode, Postgres in real mode); "postgres" with PROVIDER_MODE=mock is the
    #: demo configuration -- real persistence, no keys, no network.
    REPO_BACKEND: Literal["auto", "memory", "postgres"] = "auto"
    DATABASE_URL: str = "postgresql+psycopg://verifier:verifier@localhost:5432/verifier"
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Vendors. Blank => provider unavailable; PROVIDER_MODE=real raises at construction. ---
    VOYAGE_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    EMBEDDINGS_MODEL: str = "voyage-law-2"  # voyage-context-4 swaps in the native
    EMBEDDINGS_DIM: int = 1024  # contextual endpoint; see providers/voyage.py
    SUMMARISER_MODEL: str = "anthropic/claude-haiku-4.5"
    SUMMARISER_PROVIDER: Literal["openrouter", "anthropic"] = "openrouter"
    JUDGE_PROVIDER: Literal["openrouter", "anthropic"] = "openrouter"
    JUDGE_MODEL: str = "anthropic/claude-sonnet-5"
    #: Output budget for one judge call. The provider default of 4096 is NOT enough for
    #: current reasoning models: they spend it on internal reasoning and get cut off
    #: mid-verdict. Observed live -- Gemini 3.1 Pro returned 780 characters of truncated
    #: JSON, which the parse ladder reported as "unparseable", and L5 fails open on an
    #: unreadable verdict. So a conviction became a pass because of a token budget.
    JUDGE_MAX_TOKENS: int = 16000

    # --- The council -------------------------------------------------------------
    #: L5 is the one layer that reasons, and a single reasoning model is a single point
    #: of failure that no deterministic test can catch. These seats are polled
    #: independently -- separate requests, no seat told the others exist -- and their
    #: votes combined by JUDGE_COUNCIL_RULE.
    #:
    #: One roster note, measured rather than assumed: on a live pair the previous
    #: generation of these models split 1-4 on substantive correctness, and the current
    #: generation went 5-0 on the same answer with the same context. Model RECENCY moved
    #: the verdict far more than seat COUNT did, which is why these are pinned to
    #: flagship tiers and worth revisiting whenever a vendor ships a new one.
    #:
    #: Blank disables the council and L5 falls back to the single JUDGE_MODEL.
    JUDGE_COUNCIL: str = (
        "anthropic/claude-opus-5,"
        "openai/gpt-6-astra-pro,"
        "google/gemini-3.1-pro-preview,"
        "anthropic/claude-sonnet-5,"
        "deepseek/deepseek-v4-pro"
    )
    #: How votes become a verdict, per dimension. "majority" is the default because it
    #: is the rule that cancels a single seat's sampling noise, which is the failure a
    #: panel is actually good at fixing. Note the trade it makes: majority also
    #: OUTVOTES a lone seat that is right, so it lowers both the noise and the ceiling.
    #: "any" convicts on one dissent -- maximally strict, and in tension with the
    #: governing rule below of preferring a false green to a false red.
    JUDGE_COUNCIL_RULE: Literal["majority", "any", "unanimous"] = "majority"
    #: Seats are polled concurrently; this caps in-flight requests.
    JUDGE_COUNCIL_CONCURRENCY: int = 5
    #: Bare first-party id, NOT the OpenRouter-namespaced form. The extractor talks to
    #: the Anthropic API directly, and "anthropic/claude-haiku-4.5" split on "/" gives
    #: "claude-haiku-4.5", which that API rejects -- the id is "claude-haiku-4-5".
    EXTRACTOR_MODEL: str = "claude-haiku-4-5"
    #: Which door to Haiku. Defaults to openrouter like the judge and the summariser,
    #: because a deployment holding only an OpenRouter key would otherwise have L0
    #: permanently degraded -- every run reporting that nothing could be checked.
    EXTRACTOR_PROVIDER: Literal["openrouter", "anthropic"] = "openrouter"

    # --- Per-capability provider modes -------------------------------------------
    # "auto" follows PROVIDER_MODE. Set individually to run, say, a real judge and
    # summariser on OpenRouter while embeddings stay mocked for want of a Voyage key.
    EMBEDDINGS_MODE: Literal["auto", "mock", "real"] = "auto"
    SUMMARISER_MODE: Literal["auto", "mock", "real"] = "auto"
    JUDGE_MODE: Literal["auto", "mock", "real"] = "auto"
    #: L1a's citation finder. "auto" follows PROVIDER_MODE, which is what keeps the
    #: offline suite and every mock demo on the deterministic path at ~5 ms.
    EXTRACTOR_MODE: Literal["auto", "mock", "real"] = "auto"
    JUDGE_PROMPT_VERSION: str = "v2"
    SUMMARY_PROMPT_VERSION: str = "v1"

    # --- Thresholds -------------------------------------------------------------
    # These are REASONED SEEDS, NOT MEASUREMENTS. No cosine threshold transfers
    # across models (arXiv:2504.16318), so they are keyed by model and must be
    # recalibrated whenever EMBEDDINGS_MODEL changes. See docs/03-findings.md for
    # the 20-pair mu-2sigma procedure.
    #
    # Governing rule: PREFER A FALSE GREEN TO A FALSE RED. Fail-fast makes a false
    # FAIL unrecoverable, and wrongly accusing correct legal work is what destroys
    # trust in an accuracy tool.
    # L1a -- is the proposition supported by anything at all?
    #
    # The FAIL is a COUNT, not a judgement: zero authority of any kind anywhere in an
    # output that asserts law. There is no attribution in it and therefore nothing to
    # be wrong about, which is what lets it sit at the deterministic tier and skip the
    # judge. Per-proposition findings, where attribution IS a judgement, only WARN --
    # set L1A_UNCITED_SEVERITY=info to make them display-only.
    L1A_ENABLED: bool = True
    L1A_UNCITED_SEVERITY: Literal["warn", "info"] = "warn"
    #: How many uncited assertions an output must make before a total absence of
    #: authority is a FAIL. One is enough: a single confident statement of law resting
    #: on nothing is the failure this layer exists to catch.
    L1A_MIN_ASSERTIONS_FOR_FAIL: int = 1

    L1_QUOTE_FAIL_BELOW: float = 75.0  # rapidfuzz.partial_ratio, 0-100
    L1_QUOTE_PASS_AT: float = 90.0
    L1_MIN_QUOTE_CHARS: int = 40  # shorter strings match anything
    L1_SOFT_404_MAX_BYTES: int = 10_000  # real judgment ~150kB, soft-404 ~3.5kB (F3)
    L1_PARTY_MATCH_MIN: float = 85.0

    #: A claim shorter than this is expanded to the sentence it was cut from.
    #:
    #: The splitter is a model call and the prompt asking for self-contained claims is a
    #: request, not a guarantee. It cut "Policy considerations are applied only at the
    #: second stage, once a prima facie duty of care has been established" in half, and
    #: L3 scored the first 58 characters at 0.313 against a 0.35 floor while the whole
    #: sentence scores 0.649 (docs/03-findings.md F18). The fragment does not say the
    #: second stage OF WHAT, so it was never a proposition the answer made.
    #:
    #: Same principle as L1_MIN_QUOTE_CHARS from the other direction: below some length
    #: a unit is too thin to carry a reliable signal. There a short quotation matches
    #: anything; here a short claim matches nothing in particular.
    L3_CLAIM_MIN_CHARS: int = 80

    L3_MARGIN_FAIL_AT_OR_BELOW: float = 0.02
    L3_MARGIN_PASS_ABOVE: float = 0.08
    L3_ABSOLUTE_FLOOR: float = 0.35
    L3_BACKGROUND_SIZE: int = 200

    # --- L3 contextual prefix. MEASURED, not a preference. See docs/03-findings.md F14.
    #
    #   "none"             chunk text only
    #   "heading"          "Section: A > B" + text                        (default)
    #   "summary_heading"  "Document summary: ..." + section + text       (what shipped)
    #
    # The summary runs ~1,500 chars and is byte-identical across every chunk of a
    # judgment, so it dominates the vector and, worse, it dominates it the SAME way
    # every time. Mean pairwise cosine between Spandeck's own 43 chunks:
    #
    #     raw 0.426 | heading 0.435 | summary+heading 0.894 | voyage-context-4 0.940
    #
    # At 0.894 the 43 passages are one blurred point. Ranking inside the document is
    # all L3 scores and all of L5's evidence retrieval, so collapsing it breaks both:
    # the paragraph an answer quotes VERBATIM falls from rank #2 to #16, and a
    # correctly grounded claim falls from 0.392 to 0.325, under the 0.35 floor.
    #
    # The heading path costs 2% of mean similarity and fails nothing; the summary
    # costs 23% and fails correct legal work. Hence: keep one, drop the other.
    #
    # This is deliberately NOT a threshold and must not be tuned to move a verdict.
    # It selects which text is embedded; the thresholds in Part 4 are calibrated
    # against raw-ish chunk text and stay where they are.
    L3_CONTEXTUAL_PREFIX: Literal["none", "heading", "summary_heading"] = "heading"

    # --- L3 retrieval breadth. NOT a threshold: no verdict depends on these. ------
    # L3 SCORES on max cos(claim, chunks) and always will -- every figure in
    # docs/03-findings.md Part 4 is calibrated against that maximum, so widening the
    # evidence set must not widen what is scored. These two govern only how much of
    # the source the JUDGE gets to read, which has no threshold attached to it.
    #
    # Retrieving one chunk per claim was the real bound on L5: the judge could only
    # ever reason over the single best-matching passage, so a decisive paragraph
    # ranked second was invisible to it and the verdict tracked whichever passage
    # happened to win. See todo.md bug 2.
    L3_PASSAGES_PER_CLAIM: int = 3

    #: Total passages handed to the judge, across every claim and citation. One
    #: setting because L3 fills the list and L5 renders it, and two constants drifting
    #: apart silently truncates evidence in between.
    MAX_JUDGE_PASSAGES: int = 24

    #: Hard character budget per passage in the judge prompt. A chunk longer than this
    #: is split back into its own paragraphs and the best-matching ones are sent, so
    #: what the judge loses is chosen by relevance rather than by byte offset. Measured
    #: on Spandeck: 22 of 43 chunks exceed this, median 2,042 chars and max 7,103, so
    #: blind truncation was discarding most of the evidence for half the corpus.
    JUDGE_PASSAGE_MAX_CHARS: int = 1800

    # MEASURED against voyage-law-2, not a seed. Question -> answer, input_type
    # query/document, over 5 on-point answers, 4 hard negatives (same area of law,
    # different question) and 2 off-topic:
    #
    #   on-point   mu=0.528  sd=0.119  min=0.434
    #   hard-neg   mu=0.280  sd=0.055  max=0.369
    #   off-topic  mu=0.196            max=0.235
    #
    # The threshold sits in the gap between on-point min (0.434) and hard-negative max
    # (0.369): zero false fails on correct answers, all four hard negatives caught.
    #
    # The previous 0.50 seed failed THREE OF FIVE correct answers -- a reminder that a
    # plausible-looking threshold is not a measured one, and that under fail-fast an
    # over-tight threshold silently rejects good legal work.
    #
    # Caveat: n=11 total. This is a working calibration, not a benchmark. Widen the
    # sample before relying on it, and re-run it for any other embedding model.
    L4_FAIL_BELOW: float = 0.40
    L4_PASS_AT: float = 0.45
    L4_MIN_ANSWER_TOKENS: int = 20  # "Yes." scores erratically -> WARN, not FAIL

    # --- Chunking ---
    # How a source judgment is cut into retrieval units.
    #
    # "grouped" merges paragraphs greedily up to CHUNK_TARGET_TOKENS. "paragraph"
    # emits one unit per paragraph, merging only the stubs ("I agree.") forward.
    #
    # The distinction matters because 1,800 was chosen as a CEILING and then used as a
    # TARGET. F9 established that a judgment (~21k tokens) does not fit voyage-law-2's
    # 16k context, so chunking is mandatory -- but "fits the model" and "is the right
    # size to retrieve against a one-sentence claim" are different questions, and only
    # the first one was ever answered. Measured on Spandeck: 43 grouped chunks, median
    # 2,042 chars and ~6 paragraphs each, against a median paragraph of 338 chars.
    # Default chosen for EVIDENCE PRECISION, not for scores. Against real voyage-law-2
    # on the fixed calibration set (scripts/l3_probe.py), at prefix_mode=none:
    #
    #   strategy     genuine min   foreign max   GAP
    #   grouped         0.640         0.254     +0.386
    #   paragraph       0.700         0.320     +0.380
    #
    # Every score rises and the gap does not -- finer chunks match unrelated material
    # better too. So this buys no discrimination, and must not be justified as though
    # it did. What it buys is the passage L5 reasons over: the unit for [83] is [83-83]
    # rather than [83-86], provenance is exact so "at [N]" names the text actually
    # supplied, and paragraph [115] moves from rank #3 to #1. L5's reliability is
    # bounded by retrieval quality, and that bound is what this moves.
    #
    # It is also the regime Part 4's L3 thresholds were derived in, since those were
    # measured against raw paragraphs.
    #
    # Costs ~4x the vectors per document (170 chunks vs 43 on Spandeck), paid once per
    # judgment and cached forever. Revert with CHUNK_STRATEGY=grouped.
    CHUNK_STRATEGY: Literal["grouped", "paragraph"] = "paragraph"
    #: Caps a unit in BOTH modes -- an oversized single paragraph is still split
    #: here, because at that size it is no longer a good retrieval unit either.
    #: What "paragraph" changes is when a unit is CLOSED, not how large one may be.
    CHUNK_TARGET_TOKENS: int = 1800

    #: Below this, a paragraph is merged into the next one instead of standing alone.
    #: Judgments are full of "I agree." and "The appeal is dismissed." -- embedded by
    #: themselves those are noise that can out-rank substantive text, the same reason
    #: L1 refuses to score a quotation under L1_MIN_QUOTE_CHARS.
    #:
    #: A section boundary still wins over it: merging across a heading change would
    #: give the chunk a heading path true of only half its text. So a short fragment at
    #: the end of a section is emitted short rather than joined to the next section --
    #: 7 of Spandeck's 170 paragraph-mode chunks. Kept rather than dropped: they are
    #: real text from a source we are verifying against, and a max-over-chunks score is
    #: only harmed by a short chunk if it out-ranks a substantive one.
    CHUNK_MIN_CHARS: int = 200

    CHUNK_OVERLAP_TOKENS: int = 100
    SUMMARY_MAX_TOKENS: int = 250

    # --- Source politeness. We are scraping a public court site; do not remove. ---
    SOURCE_MAX_CONCURRENCY: int = 2
    SOURCE_MIN_INTERVAL_MS: int = 250
    SOURCE_TIMEOUT_S: float = 20.0
    SOURCE_USER_AGENT: str = "sal-verifier/0.1 (SMU LIT 2026 research prototype)"
    ELITIGATION_BASE_URL: str = "https://www.elitigation.sg"

    # --- Browser (login-walled sources) ---
    BROWSER_PROFILE_DIR: str = "./browser-profile"
    BROWSER_TIMEOUT_S: float = 45.0
    BROWSER_HEADLESS: bool = True

    # --- L1a extractor. Operational, NOT thresholds: no verdict may be tuned here. ---
    #: L0 sits on the fast path, and the Anthropic SDK's own default is ten minutes.
    #: A slow extractor must degrade to "we did not find out", not hold the run open.
    EXTRACTOR_TIMEOUT_S: float = 15.0
    EXTRACTOR_PROMPT_VERSION: str = "v1"

    # --- Reliability ---
    TASK_MAX_RETRIES: int = 2
    RESOLUTION_TTL_HOURS: int = 168

    # --- Mock-embedder thresholds -----------------------------------------------
    # NOT a special case in the code -- this IS the design principle applied.
    # Thresholds are keyed by model and do not transfer (arXiv:2504.16318), and the
    # hashed bag-of-words mock is a different model from voyage-law-2. It has no
    # synonymy, so a genuine paraphrase scores far lower than it would under a real
    # legal embedding model.
    #
    # Measured over 4 on-point and 4 off-point Singapore-law answers:
    #   on-point   mu=0.270  sd=0.144  min=0.145
    #   off-point  mu=0.039  sd=0.067  max=0.155
    # 0.08 / 0.20 gives ZERO false fails on the on-point set while still failing 3 of
    # 4 off-point answers -- the correct trade under fail-fast, where a false FAIL is
    # unrecoverable. Applying the real-model 0.50/0.70 here fails EVERY answer, which
    # would paint a green run red on L4 alone.
    # Lowered from 0.08 after a live run: a correctly-worded on-point answer scored
    # 0.073 and was failed. The original figure came from a 4-sample estimate whose
    # on-point minimum was 0.145, so the real spread is wider than that sample showed.
    # Under fail-fast a false FAIL is unrecoverable, so the threshold sits below the
    # lowest on-point score actually observed, not below the estimated one.
    MOCK_L4_FAIL_BELOW: float = 0.04
    MOCK_L4_PASS_AT: float = 0.20

    # L3 under the mock embedder. Measured, claim -> cited document, over 3 genuine
    # Spandeck claims and 2 from an unrelated area of law:
    #   genuine  cited min 0.151   margin min +0.012
    #   foreign  cited max 0.105   margin max +0.040
    #
    # The absolute score separates (0.151 vs 0.105), so the floor drops to 0.12 --
    # the real-model 0.35 fails every genuine claim.
    #
    # THE MARGIN DOES NOT SEPARATE and is switched off here: a genuine claim scored
    # +0.012 while a foreign one scored +0.040, so the contrastive signal is
    # anti-discriminative under a hashed bag-of-words with no synonymy. It is a real
    # signal for a real embedding model and a coin flip for this one, so mock mode
    # leans on the absolute floor alone rather than pretending otherwise.
    MOCK_L3_ABSOLUTE_FLOOR: float = 0.12
    MOCK_L3_MARGIN_FAIL_AT_OR_BELOW: float = -1.0
    MOCK_L3_MARGIN_PASS_ABOVE: float = -0.99

    @model_validator(mode="after")
    def _apply_mock_thresholds(self) -> Settings:
        """In mock mode, use mock-calibrated thresholds unless explicitly overridden.

        Only fields the caller did not set are replaced, so an explicit env var or
        constructor argument always wins -- tests that pin a threshold keep pinning it.
        """
        # Keyed on the EMBEDDER, not PROVIDER_MODE: L3/L4 thresholds describe the
        # embedding model, so a real judge running alongside mock embeddings must
        # still use the mock's numbers.
        if self.capability_is_real("embeddings"):
            return self
        for field, mock_field in (
            ("L4_FAIL_BELOW", "MOCK_L4_FAIL_BELOW"),
            ("L4_PASS_AT", "MOCK_L4_PASS_AT"),
            ("L3_ABSOLUTE_FLOOR", "MOCK_L3_ABSOLUTE_FLOOR"),
            ("L3_MARGIN_FAIL_AT_OR_BELOW", "MOCK_L3_MARGIN_FAIL_AT_OR_BELOW"),
            ("L3_MARGIN_PASS_ABOVE", "MOCK_L3_MARGIN_PASS_ABOVE"),
        ):
            if field not in self.model_fields_set:
                object.__setattr__(self, field, getattr(self, mock_field))
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_mock(self) -> bool:
        return self.PROVIDER_MODE == "mock"

    @property
    def council_models(self) -> tuple[str, ...]:
        """The council roster, de-duplicated, order preserved.

        Order is meaningful: it is the order seats are reported in, and a reader
        comparing two runs should see the same seat in the same place. Duplicates are
        dropped because polling one model twice is not a second opinion -- it is the
        same opinion resampled, which the measurements showed does not move a
        systematic disagreement at all.
        """
        return tuple(dict.fromkeys(m.strip() for m in self.JUDGE_COUNCIL.split(",") if m.strip()))

    @property
    def uses_postgres(self) -> bool:
        """Whether the Postgres repos are in play, independent of vendor mode."""
        if self.REPO_BACKEND == "auto":
            return not self.is_mock
        return self.REPO_BACKEND == "postgres"

    def capability_is_real(
        self, capability: Literal["embeddings", "summariser", "judge", "extractor"]
    ) -> bool:
        """Whether one capability should use its real provider.

        PROVIDER_MODE is the default for all three, but a single global switch forces
        an all-or-nothing choice: holding one vendor's key would otherwise mean either
        running everything mocked, or running everything real and failing at
        construction on the key you do not have. "auto" follows PROVIDER_MODE.
        """
        mode = {
            "embeddings": self.EMBEDDINGS_MODE,
            "summariser": self.SUMMARISER_MODE,
            "judge": self.JUDGE_MODE,
            "extractor": self.EXTRACTOR_MODE,
        }[capability]
        if mode == "auto":
            return not self.is_mock
        return mode == "real"

    @model_validator(mode="after")
    def _require_keys_in_real_mode(self) -> Settings:
        """Fail loudly rather than silently falling back to mocks in production."""
        if self.PROVIDER_MODE == "real" and self.ENV == "production":
            missing = [
                name for name in ("VOYAGE_API_KEY", "DATABASE_URL") if not getattr(self, name)
            ]
            judge_key = (
                "OPENROUTER_API_KEY" if self.JUDGE_PROVIDER == "openrouter" else "ANTHROPIC_API_KEY"
            )
            if not getattr(self, judge_key):
                missing.append(judge_key)
            if missing:
                raise ValueError("PROVIDER_MODE=real in production requires: " + ", ".join(missing))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
