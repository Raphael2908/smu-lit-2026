"""Autouse fixtures that make the whole backend exercisable offline.

Sockets are blocked, providers are mocked and settings are forced to mock mode. If a
test can reach the network, that is a bug in this file, not a convenience.
"""

from __future__ import annotations

import os

import pytest

# Pin the whole provider surface BEFORE anything imports settings.
#
# setdefault is not enough and os.environ is not enough on its own: pydantic-settings
# also reads .env, and a developer with real keys configured there would otherwise run
# the suite against live vendors -- slow, billable, and non-deterministic. A test suite
# whose result depends on an untracked file is not a test suite. Explicit environment
# variables take precedence over .env, so setting every mode here makes the run
# hermetic regardless of local configuration.
os.environ["ENV"] = "test"
os.environ["PROVIDER_MODE"] = "mock"
os.environ["EMBEDDINGS_MODE"] = "mock"
os.environ["SUMMARISER_MODE"] = "mock"
os.environ["JUDGE_MODE"] = "mock"
os.environ["REPO_BACKEND"] = "memory"
# Blank the keys too, so a real provider constructed by mistake fails loudly with
# ProviderKeyMissing instead of quietly spending the user's money.
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["VOYAGE_API_KEY"] = ""


@pytest.fixture(autouse=True)
def _no_network(socket_enabled: bool = False):
    """pytest-socket blocks real sockets for the whole suite."""
    from pytest_socket import disable_socket, enable_socket

    disable_socket(allow_unix_socket=True)
    yield
    enable_socket()


@pytest.fixture(autouse=True)
def _mock_providers():
    """Force mock mode and clear the provider caches around every test."""
    from verifier.providers import factory
    from verifier.settings import get_settings

    get_settings.cache_clear()
    factory.reset_provider_cache()
    yield
    factory.reset_provider_cache()
    get_settings.cache_clear()


@pytest.fixture
def settings():
    from verifier.settings import get_settings

    return get_settings()
