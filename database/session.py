"""Асинхронный движок и фабрика сессий SQLAlchemy."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import BASE_DIR, settings
from database.migrations import migrate_schema
from database.models import Base

logger = logging.getLogger(__name__)


def _resolved_database_url(database_url: str):
    """Превращает относительный путь SQLite в абсолютный от корня проекта."""
    url = make_url(database_url)
    if url.drivername.startswith("sqlite") and url.database and url.database != ":memory:":
        path = Path(url.database)
        if not path.is_absolute():
            path = (BASE_DIR / path).resolve()
        return url.set(database=str(path))
    return url


def _prepare_sqlite_directory(database_url) -> None:  # type: ignore[no-untyped-def]
    if not database_url.drivername.startswith("sqlite"):
        return
    database = database_url.database
    if not database or database == ":memory:":
        return
    Path(database).parent.mkdir(parents=True, exist_ok=True)


_url = _resolved_database_url(settings.DATABASE_URL)
_prepare_sqlite_directory(_url)

engine = create_async_engine(
    _url,
    echo=False,
    pool_pre_ping=True,
)

if _url.drivername.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        applied = await connection.run_sync(migrate_schema)
        if applied:
            logger.info("schema_migrations_applied count=%s changes=%s", len(applied), applied)
    if _url.drivername.startswith("sqlite"):
        logger.info("database_ready sqlite=%s", _url.database)


async def dispose_db() -> None:
    await engine.dispose()
