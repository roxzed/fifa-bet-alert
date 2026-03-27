"""
Database engine, session factory and initialization for FIFA Bet Alert.

Uses SQLAlchemy 2.0 async with asyncpg (PostgreSQL / Supabase).
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from loguru import logger

from src.config import settings
from src.db.models import Base

# Detect database type
_is_sqlite = "sqlite" in settings.database_url

_connect_args = {"check_same_thread": False} if _is_sqlite else {}

_pool_kwargs = {}
if not _is_sqlite:
    _pool_kwargs = {
        "pool_size": 10,
        "max_overflow": 10,
        "pool_timeout": 10,        # fail fast — não esperar 30s por conexão
        "pool_recycle": 180,       # reciclar a cada 3 min (Supabase mata idle ~5 min)
        "pool_pre_ping": True,     # testa conexão antes de usar
    }
    # Statement timeout de 15s para evitar queries penduradas
    _connect_args = {
        "server_settings": {"statement_timeout": "15000"},
        "command_timeout": 15,
    }

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args=_connect_args,
    **_pool_kwargs,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a database session."""
    session = async_session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """Create all tables defined in the ORM models.

    Safe to call multiple times -- uses CREATE IF NOT EXISTS under the hood.
    """
    logger.info("Initializing database at {}", settings.database_url.split("@")[-1] if "@" in settings.database_url else settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created successfully")


async def close_db() -> None:
    """Dispose of the engine connection pool."""
    await engine.dispose()
    logger.info("Database engine disposed")
