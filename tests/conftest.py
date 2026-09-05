"""Autouse fixtures that make the whole backend exercisable offline.

Sockets are blocked, providers are mocked and settings are forced to mock mode. If a
test can reach the network, that is a bug in this file, not a convenience.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("PROVIDER_MODE", "mock")
os.environ.setdefault("ENV", "test")


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
