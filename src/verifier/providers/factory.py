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
def get_http_fetcher(user_agent: str | None = None) -> Fetcher:
    """``user_agent`` is per-SOURCE, not per-deployment.

    Sources disagree about what a polite client looks like. eLitigation accepts
    ``sigma-tech/0.1 (...)``; sso.agc.gov.sg answers that exact string with 403 and
    serves ``Mozilla/5.0 (compatible; sigma-tech/0.1; ...)`` -- the conventional
    ``(compatible; ...)`` bot form -- with 200. Both identify us honestly; only one is
    shaped the way that WAF expects. One global default could not satisfy both.

    Callers must always pass the argument rather than calling bare: ``f()`` and ``f(None)``
    are different ``lru_cache`` keys and would build two clients.
    """
    if settings.is_mock:
        from verifier.providers.mock.fetcher import MockFetcher

        return MockFetcher()
    from verifier.providers.fetcher_http import HttpFetcher

    return HttpFetcher(user_agent=user_agent)


@lru_cache
def get_browser_fetcher(user_agent: str | None = None) -> Fetcher:
    """Sources with no plain-HTTP path. Heavy: keep it off the fast path.

    Two kinds qualify, and they are different problems with one answer. A LOGIN WALL
    (LawNet) needs a persisted session a script cannot obtain. A BOT-DETECTION CHALLENGE
    (AGC SSO answers httpx with 202 and ``x-amzn-waf-action: challenge``) needs a client
    that executes the page. Both are "a real browser or nothing".

    ``user_agent`` is None for "send whatever Chromium sends", which is the right default
    -- see ``BrowserFetcher._context_kwargs``. Callers must always pass the argument
    rather than calling bare: ``f()`` and ``f(None)`` are different ``lru_cache`` keys.
    """
    if settings.is_mock:
        from verifier.providers.mock.fetcher import MockFetcher

        return MockFetcher(strategy="browser")
    from verifier.providers.fetcher_browser import BrowserFetcher

    return BrowserFetcher(user_agent=user_agent)


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
