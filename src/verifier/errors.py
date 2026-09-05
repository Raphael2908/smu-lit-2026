"""Error hierarchy. The retry policy hinges on retryable vs fatal."""

from __future__ import annotations


class VerifierError(Exception):
    """Base for everything this system raises deliberately."""


class ProviderError(VerifierError):
    pass


class RetryableError(ProviderError):
    """429 / 5xx / timeout -- safe to retry with backoff."""


class FatalError(ProviderError):
    """4xx validation / policy -- retrying will not help."""


class ProviderKeyMissing(FatalError):
    """A real provider was constructed without its key. Never fall back to a mock
    silently: a verifier that quietly stops verifying is worse than one that stops."""

    def __init__(self, provider: str, env_var: str) -> None:
        super().__init__(f"{provider} requires {env_var}. Set it, or run with PROVIDER_MODE=mock.")


class SourceUnauthenticated(ProviderError):
    """A login-walled source rejected us. Maps to WARN, never FAIL -- being unable to
    check a citation is not evidence that it was fabricated."""


class LayerError(VerifierError):
    pass


class ContractViolation(VerifierError):
    """An invariant that must never break broke. Notably: the judge attempting to
    upgrade a deterministic failure."""
