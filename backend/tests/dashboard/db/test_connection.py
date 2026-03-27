"""
Phase 1 — Database Connection Tests

Tests the database connection management utilities: engine creation,
session factory, and init_db function.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from alpha_lab.dashboard.db.connection import create_db_engine, get_session_factory, init_db
from alpha_lab.dashboard.db.models import Base, Config

pytestmark = pytest.mark.asyncio


async def test_create_engine_returns_async_engine():
    """create_db_engine returns an AsyncEngine instance."""
    engine = create_db_engine("sqlite+aiosqlite://")
    assert isinstance(engine, AsyncEngine)
    await engine.dispose()


async def test_get_session_factory_produces_sessions():
    """get_session_factory returns a factory that creates AsyncSession instances."""
    engine = create_db_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = get_session_factory(engine)
    async with factory() as session:
        assert isinstance(session, AsyncSession)

    await engine.dispose()


async def test_init_db_creates_tables():
    """init_db creates all tables from the model metadata."""
    engine = create_db_engine("sqlite+aiosqlite://")
    await init_db(engine)

    factory = get_session_factory(engine)
    async with factory() as session:
        # Should be able to insert into config table
        session.add(Config(key="test", value={"ok": True}))
        await session.commit()

    await engine.dispose()
