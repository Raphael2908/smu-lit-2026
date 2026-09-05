"""Default wiring for the semantic layers.

``registry.build_layer`` constructs layers with no arguments, so L3 and L4 need
sensible defaults for their collaborators while still accepting injection. Everything
here is a seam: the composition root swaps the in-memory repos for the Postgres ones
without either layer noticing.
"""

from __future__ import annotations

from typing import Any

from verifier.logging import get_logger
from verifier.providers.base import Embedder, Summariser
from verifier.repos.base import DocumentRepo, EmbeddingRepo
from verifier.repos.memory import InMemoryDocumentRepo, InMemoryEmbeddingRepo

log = get_logger(__name__)


class _Default:
    """Sentinel for 'argument not supplied'.

    Needed because ``None`` is a meaningful value for every collaborator these layers
    take: ``summariser=None`` means "deliberately run with no LLM", and
    ``embedding_repo=None`` means "deliberately run with no cache". Without a sentinel
    those two intentions are indistinguishable from "use the default".
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<default>"


DEFAULT: Any = _Default()


def resolve(value: Any, factory: Any) -> Any:
    return factory() if isinstance(value, _Default) else value


_document_repo: DocumentRepo | None = None
_embedding_repo: EmbeddingRepo | None = None


def default_embedder() -> Embedder:
    from verifier.providers.factory import get_embedder

    return get_embedder()


def default_summariser() -> Summariser | None:
    """The summariser, or None when the LLM tier is not wired up yet.

    Returning None is a supported state, not a failure: claim splitting falls back to
    deterministic sentence windows and contextualisation falls back to bare chunk text.
    The semantic layers are deliberately able to run with no LLM in the loop at all --
    that is what lets L3 and L4 answer at t=0 while the judge is still cold.
    """
    try:
        from verifier.providers.factory import get_summariser

        return get_summariser()
    except Exception:  # noqa: BLE001 - provider not yet available; degrade, never crash
        return None


def default_document_repo() -> DocumentRepo:
    global _document_repo
    if _document_repo is None:
        _document_repo = InMemoryDocumentRepo()
    return _document_repo


def default_embedding_repo() -> EmbeddingRepo:
    """The process's embedding cache, taken from the repo bundle.

    Memoised across layer instances on purpose: a per-instance cache would report a
    100% miss rate forever and, worse, leave L3's background pool permanently empty.

    The bundle is what makes the cache DURABLE. ``get_repos()`` returns a
    ``PgEmbeddingRepo`` under ``REPO_BACKEND=postgres`` and an ``InMemoryEmbeddingRepo``
    otherwise, so this one line is the whole of the module docstring's promise that the
    composition root swaps the repos without either layer noticing. It was never wired:
    ``registry.build_layer`` builds L3 and L4 with no arguments, so both fell through to
    a process-local dict while ``build_pg_repos()`` constructed a Postgres repo nothing
    ever read. Two caches side by side, neither shared between workers, so every run
    re-embedded the whole judgment -- ~43 s of the 46 s that overran the run budget.

    Falling back to memory rather than raising is deliberate, and matches
    ``Orchestrator._repos()``: a database that is not up is a slow verifier, never a
    broken one.
    """
    global _embedding_repo
    if _embedding_repo is None:
        _embedding_repo = _bundle_embedding_repo()
    return _embedding_repo


def _bundle_embedding_repo() -> EmbeddingRepo:
    try:
        from verifier.repos.pg import get_repos

        return get_repos().embeddings
    except Exception as exc:  # noqa: BLE001 - a cache is never a verdict
        log.warning("embedding_repo_unavailable", error=str(exc))
        return InMemoryEmbeddingRepo()


def reset_default_repos() -> None:
    """Drop the process-local repos. Tests that rely on a cold cache call this."""
    global _document_repo, _embedding_repo
    _document_repo = None
    _embedding_repo = None
