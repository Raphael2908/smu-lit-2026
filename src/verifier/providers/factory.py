"""Provider selection. ALL branches are written up front, each real impl imported
lazily so the API tier never pulls a vendor SDK it will not use.

Every parallel workstream edits exactly one line here, which is why this file exists
in full on day zero: it is touched by everyone and therefore merges cleanly only if
nobody has to add structure to it.
"""

from __future__ import annotations

from functools import lru_cache

from verifier.providers.base import CitationExtractor, Embedder, Fetcher, Judge, Summariser
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
    if not settings.capability_is_real("embeddings"):
        from verifier.providers.mock.embeddings import MockEmbedder

        return MockEmbedder()
    from verifier.providers.voyage import VoyageEmbedder

    return VoyageEmbedder()


@lru_cache
def get_summariser() -> Summariser:
    if not settings.capability_is_real("summariser"):
        from verifier.providers.mock.llm import MockSummariser

        return MockSummariser()
    if settings.SUMMARISER_PROVIDER == "anthropic":
        from verifier.providers.anthropic_llm import AnthropicSummariser

        return AnthropicSummariser()
    from verifier.providers.openrouter_llm import OpenRouterSummariser

    return OpenRouterSummariser()


@lru_cache
def get_judge() -> Judge:
    if not settings.capability_is_real("judge"):
        from verifier.providers.mock.llm import MockJudge

        return MockJudge()
    # A council of one is a single judge with extra indirection, so it is not built --
    # and the roster is deliberately checked before JUDGE_PROVIDER, because the seats
    # are OpenRouter ids and a panel is a cross-vendor construct that the first-party
    # Anthropic door cannot serve.
    if len(settings.council_models) > 1:
        from verifier.providers.council import council_from_settings

        return council_from_settings()
    if settings.JUDGE_PROVIDER == "anthropic":
        from verifier.providers.anthropic_llm import AnthropicJudge

        return AnthropicJudge()
    from verifier.providers.openrouter_llm import OpenRouterJudge

    return OpenRouterJudge()


@lru_cache
def get_citation_extractor() -> CitationExtractor:
    """L1a's citation finder.

    The mock is not a stub: it runs the real regex extractor, so mock mode still finds
    citations with no key and no network. See MockCitationExtractor.
    """
    if not settings.capability_is_real("extractor"):
        from verifier.providers.mock.llm import MockCitationExtractor

        return MockCitationExtractor()
    if settings.EXTRACTOR_PROVIDER == "anthropic":
        from verifier.providers.anthropic_llm import AnthropicCitationExtractor

        return AnthropicCitationExtractor()
    from verifier.providers.openrouter_llm import OpenRouterCitationExtractor

    return OpenRouterCitationExtractor()


def reset_provider_cache() -> None:
    """Tests flip PROVIDER_MODE between cases; the lru_caches must follow."""
    for fn in (
        get_http_fetcher,
        get_browser_fetcher,
        get_embedder,
        get_summariser,
        get_judge,
        get_citation_extractor,
    ):
        fn.cache_clear()
