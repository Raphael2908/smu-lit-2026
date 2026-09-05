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
    DATABASE_URL: str = "postgresql+psycopg://verifier:verifier@localhost:5432/verifier"
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Vendors. Blank => provider unavailable; PROVIDER_MODE=real raises at construction. ---
    VOYAGE_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""

    EMBEDDINGS_MODEL: str = "voyage-law-2"  # voyage-context-4 swaps in the native
    EMBEDDINGS_DIM: int = 1024  # contextual endpoint; see providers/voyage.py
    SUMMARISER_MODEL: str = "claude-sonnet-5"
    JUDGE_PROVIDER: Literal["openrouter", "anthropic"] = "openrouter"
    JUDGE_MODEL: str = "anthropic/claude-opus-5"
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
    L1_QUOTE_FAIL_BELOW: float = 75.0  # rapidfuzz.partial_ratio, 0-100
    L1_QUOTE_PASS_AT: float = 90.0
    L1_MIN_QUOTE_CHARS: int = 40  # shorter strings match anything
    L1_SOFT_404_MAX_BYTES: int = 10_000  # real judgment ~150kB, soft-404 ~3.5kB (F3)
    L1_PARTY_MATCH_MIN: float = 85.0

    L3_MARGIN_FAIL_AT_OR_BELOW: float = 0.02
    L3_MARGIN_PASS_ABOVE: float = 0.08
    L3_ABSOLUTE_FLOOR: float = 0.35
    L3_BACKGROUND_SIZE: int = 200

    L4_FAIL_BELOW: float = 0.50
    L4_PASS_AT: float = 0.70
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

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_mock(self) -> bool:
        return self.PROVIDER_MODE == "mock"

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
