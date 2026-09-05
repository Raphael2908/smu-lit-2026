"""Async engine and session plumbing.

Engine construction is LAZY and never connects: SQLAlchemy engines are pools, not
connections. That is what lets the API boot with an empty ``.env`` and no database
reachable -- ``/readyz`` reports ``database: false`` instead of the process dying.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from verifier.logging import get_logger
from verifier.settings import settings

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def async_database_url(url: str | None = None) -> str:
    """Coerce a URL onto an async-capable driver.

    ``DATABASE_URL`` is shared with Alembic, which is happy with either form. A bare
    ``postgresql://`` would pick psycopg2 (sync) and blow up inside the async engine,
    so normalise it here rather than making every caller remember.
    """
    raw = url or settings.DATABASE_URL
    if raw.startswith("postgresql+"):
        return raw
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+psycopg://", 1)
    return raw


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            async_database_url(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work. Commits on success, rolls back on anything."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping(timeout: float = 1.5) -> bool:
    """Cheap liveness probe for ``/readyz``. Never raises -- a down database is a
    reportable condition, not an exception the API tier should propagate."""
    try:
        async with asyncio.timeout(timeout):
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 -- any failure means "not ready"
        log.debug("database_ping_failed", error=str(exc))
        return False


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
