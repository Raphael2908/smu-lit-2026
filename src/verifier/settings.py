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

    # --- Per-capability provider modes -------------------------------------------
    # "auto" follows PROVIDER_MODE. Set individually to run, say, a real judge and
    # summariser on OpenRouter while embeddings stay mocked for want of a Voyage key.
    EMBEDDINGS_MODE: Literal["auto", "mock", "real"] = "auto"
    SUMMARISER_MODE: Literal["auto", "mock", "real"] = "auto"
    JUDGE_MODE: Literal["auto", "mock", "real"] = "auto"
    JUDGE_PROMPT_VERSION: str = "v1"
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

    L3_MARGIN_FAIL_AT_OR_BELOW: float = 0.02
    L3_MARGIN_PASS_ABOVE: float = 0.08
    L3_ABSOLUTE_FLOOR: float = 0.35
    L3_BACKGROUND_SIZE: int = 200

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
    CHUNK_TARGET_TOKENS: int = 1800  # voyage-law-2 ctx is 16k; a judgment is ~21k (F9)
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
    def uses_postgres(self) -> bool:
        """Whether the Postgres repos are in play, independent of vendor mode."""
        if self.REPO_BACKEND == "auto":
            return not self.is_mock
        return self.REPO_BACKEND == "postgres"

    def capability_is_real(self, capability: Literal["embeddings", "summariser", "judge"]) -> bool:
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
