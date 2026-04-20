"""
StreamScout & GigHunt — Database Engine & Session
===================================================
Uses **SQLite** by default (zero install, file-based).
Supports PostgreSQL if DATABASE_URL is overridden in ``.env``.

Provides both async (FastAPI) and sync (Celery workers) sessions.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings
from models import Base

_settings = get_settings()

# ── Detect SQLite vs PostgreSQL ──────────────────────────────
_is_sqlite = _settings.database_url.startswith("sqlite")

# ── Async engine (FastAPI) ───────────────────────────────────

_engine_kwargs: dict = {
    "echo": _settings.debug,
}

if not _is_sqlite:
    # PostgreSQL supports connection pooling
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)

async_engine = create_async_engine(_settings.database_url, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ── Sync engine (Celery workers) ─────────────────────────────

_sync_kwargs: dict = {
    "echo": _settings.debug,
}

if not _is_sqlite:
    _sync_kwargs.update(pool_size=5, max_overflow=10, pool_pre_ping=True)

sync_engine = create_engine(_settings.database_url_sync, **_sync_kwargs)

# Enable WAL mode for SQLite (better concurrent read/write)
if _is_sqlite:
    @event.listens_for(sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    class_=Session,
    expire_on_commit=False,
)


# ── FastAPI dependency ───────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yield an async DB session for a single request.

    Usage in a FastAPI route::

        @router.get("/example")
        async def example(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Table creation (first-run bootstrap) ─────────────────────

async def init_db() -> None:
    """Create all tables if they don't exist yet."""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_sync_session():
    """Provide a sync session for Celery tasks (context manager)."""
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
