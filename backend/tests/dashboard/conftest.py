"""
Shared fixtures for dashboard tests.

Uses SQLite async in-memory database for test isolation.
No PostgreSQL required for unit tests.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alpha_lab.dashboard.db.models import Base


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def async_engine():
    """Create an in-memory SQLite async engine for testing."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def async_session(async_engine) -> AsyncSession:
    """Provide a transactional async session for testing."""
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
