"""Provider selection. ALL branches are written up front, each real impl imported
lazily so the API tier never pulls a vendor SDK it will not use.

Every parallel workstream edits exactly one line here, which is why this file exists
in full on day zero: it is touched by everyone and therefore merges cleanly only if
nobody has to add structure to it.
"""

from __future__ import annotations

from functools import lru_cache

from verifier.providers.base import Embedder, Fetcher, Judge, Summariser
from verifier.settings import settings


@lru_cache
def get_http_fetcher() -> Fetcher:
    if settings.is_mock:
        from verifier.providers.mock.fetcher import MockFetcher

        return MockFetcher()
    from verifier.providers.fetcher_http import HttpFetcher

    return HttpFetcher()


@lru_cache
def get_browser_fetcher() -> Fetcher:
    """Login-walled sources only. Heavy: keep it off the fast path."""
    if settings.is_mock:
        from verifier.providers.mock.fetcher import MockFetcher

        return MockFetcher(strategy="browser")
    from verifier.providers.fetcher_browser import BrowserFetcher

    return BrowserFetcher()


@lru_cache
def get_embedder() -> Embedder:
    if settings.is_mock:
        from verifier.providers.mock.embeddings import MockEmbedder

        return MockEmbedder()
    from verifier.providers.voyage import VoyageEmbedder

    return VoyageEmbedder()


@lru_cache
def get_summariser() -> Summariser:
    if settings.is_mock:
        from verifier.providers.mock.llm import MockSummariser

        return MockSummariser()
    from verifier.providers.anthropic_llm import AnthropicSummariser

    return AnthropicSummariser()


@lru_cache
def get_judge() -> Judge:
    if settings.is_mock:
        from verifier.providers.mock.llm import MockJudge

        return MockJudge()
    if settings.JUDGE_PROVIDER == "anthropic":
        from verifier.providers.anthropic_llm import AnthropicJudge

        return AnthropicJudge()
    from verifier.providers.openrouter_llm import OpenRouterJudge

    return OpenRouterJudge()


def reset_provider_cache() -> None:
    """Tests flip PROVIDER_MODE between cases; the lru_caches must follow."""
    for fn in (
        get_http_fetcher,
        get_browser_fetcher,
        get_embedder,
        get_summariser,
        get_judge,
    ):
        fn.cache_clear()
