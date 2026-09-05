"""Repository selection -- the one place that decides memory vs Postgres.

Every layer, the pipeline and the API talk to the protocols in ``repos/base.py``, so
this swap is invisible to all of them. That is the whole point of the repo pattern
here: the offline test suite and the demo run the same code path.

Selection rule: ``PROVIDER_MODE=mock`` implies in-memory storage. There is no separate
STORAGE setting, and inventing one would give us two knobs that must agree. Mock mode
already means "this process is self-contained, no external service required", and a
database is an external service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from verifier.logging import get_logger
from verifier.repos.base import DocumentRepo, EmbeddingRepo, ListRepo, ResolutionRepo, RunRepo
from verifier.settings import settings

log = get_logger(__name__)


@dataclass(frozen=True)
class Repos:
    """One bundle so callers pass a single object instead of five."""

    documents: DocumentRepo
    resolutions: ResolutionRepo
    embeddings: EmbeddingRepo
    runs: RunRepo
    lists: ListRepo


def _build_list_repo(*, postgres: bool) -> ListRepo:
    """The ``ListRepo`` implementation belongs to another workstream.

    Import it lazily and fall back to the in-memory one, so the API and the pipeline
    both start cleanly whether or not ``repos/lists.py`` has landed yet.
    """
    if postgres:
        try:
            from verifier.repos import lists as lists_module

            for name in ("PgListRepo", "PostgresListRepo", "ListRepoPg", "SqlListRepo"):
                impl = getattr(lists_module, name, None)
                if impl is not None:
                    return impl()  # type: ignore[no-any-return]
            log.warning("list_repo_impl_not_found", module="verifier.repos.lists")
        except ImportError:
            log.info("list_repo_module_missing", fallback="InMemoryListRepo")

    from verifier.repos.memory import InMemoryListRepo

    return InMemoryListRepo()


def build_pg_repos() -> Repos:
    from verifier.repos.documents import PgDocumentRepo
    from verifier.repos.embeddings import PgEmbeddingRepo
    from verifier.repos.resolutions import PgResolutionRepo
    from verifier.repos.runs import PgRunRepo

    return Repos(
        documents=PgDocumentRepo(),
        resolutions=PgResolutionRepo(),
        embeddings=PgEmbeddingRepo(),
        runs=PgRunRepo(),
        lists=_build_list_repo(postgres=True),
    )


def build_memory_repos() -> Repos:
    from verifier.repos.memory import (
        InMemoryDocumentRepo,
        InMemoryEmbeddingRepo,
        InMemoryResolutionRepo,
        InMemoryRunRepo,
    )

    return Repos(
        documents=InMemoryDocumentRepo(),
        resolutions=InMemoryResolutionRepo(),
        embeddings=InMemoryEmbeddingRepo(),
        runs=InMemoryRunRepo(),
        lists=_build_list_repo(postgres=False),
    )


_repos: Repos | None = None


def get_repos() -> Repos:
    """Process-wide singleton. In-memory repos ARE the store, so a second bundle would
    silently create a second, empty database."""
    global _repos
    if _repos is None:
        use_pg = settings.uses_postgres
        _repos = build_pg_repos() if use_pg else build_memory_repos()
        log.info(
            "repos_selected",
            backend="postgres" if use_pg else "memory",
            repo_backend=settings.REPO_BACKEND,
            provider_mode=settings.PROVIDER_MODE,
        )
    return _repos


def set_repos(repos: Repos | None) -> None:
    """Tests and the worker override the bundle; ``None`` resets to the default."""
    global _repos
    _repos = repos


async def ping_database(timeout: float = 1.5) -> bool:
    """``/readyz`` probe.

    Keyed on whether Postgres is actually in use, not on vendor mode. Reporting
    ``database: false`` while a healthy Postgres serves every request would make
    /readyz worse than useless -- an operator checking it before a demo would read a
    working system as broken.
    """
    if not settings.uses_postgres:
        return False
    from verifier.repos.session import ping

    return await ping(timeout)


def repo_supports(repo: Any, method: str) -> bool:
    """The in-memory and Postgres run repos both carry ``register_key``, which is not
    on the protocol. Check rather than assume, so a third implementation cannot crash
    the verify handler."""
    return callable(getattr(repo, method, None))
