"""
Database connection management.

Provides async engine creation, session factory, and initialization.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alpha_lab.dashboard.db.models import Base


def create_db_engine(url: str, **kwargs) -> AsyncEngine:
    """Create an async SQLAlchemy engine."""
    return create_async_engine(url, **kwargs)


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to the given engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create all tables from the model metadata."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
