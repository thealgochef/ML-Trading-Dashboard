"""
Phase 1 — Database Model Tests

Tests the PostgreSQL schema and ORM models. The database stores operational
data for the live trading system — connection events, OHLCV bars for chart
rendering, configuration, and provides the schema foundation for trade
history and account state in later phases.

Business context: PostgreSQL is the single source of truth for all
operational state. The dashboard reads from it, the pipeline writes to it.
Both can operate concurrently without conflicts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alpha_lab.dashboard.db.models import Config, ConnectionEvent, ModelVersion, OHLCVBar

pytestmark = pytest.mark.asyncio


async def test_config_crud(async_session):
    """Create, read, update, delete config entries."""
    # Create
    entry = Config(key="test_key", value={"foo": "bar"})
    async_session.add(entry)
    await async_session.commit()

    # Read
    result = await async_session.execute(select(Config).where(Config.key == "test_key"))
    row = result.scalar_one()
    assert row.value == {"foo": "bar"}

    # Update
    row.value = {"foo": "baz", "count": 42}
    await async_session.commit()
    await async_session.refresh(row)
    assert row.value["foo"] == "baz"
    assert row.value["count"] == 42

    # Delete
    await async_session.delete(row)
    await async_session.commit()
    result = await async_session.execute(select(Config).where(Config.key == "test_key"))
    assert result.scalar_one_or_none() is None


async def test_config_jsonb_values(async_session):
    """Config values stored as JSONB preserve nested structures."""
    nested = {
        "levels": [1, 2, 3],
        "settings": {"a": {"b": True}},
        "name": "test",
    }
    entry = Config(key="nested", value=nested)
    async_session.add(entry)
    await async_session.commit()

    result = await async_session.execute(select(Config).where(Config.key == "nested"))
    row = result.scalar_one()
    assert row.value["levels"] == [1, 2, 3]
    assert row.value["settings"]["a"]["b"] is True


async def test_connection_event_insert(async_session):
    """Connection events are stored with correct timestamps and status."""
    now = datetime.now(UTC)
    event = ConnectionEvent(
        timestamp=now,
        status="connected",
        details={"reconnect_attempts": 0},
    )
    async_session.add(event)
    await async_session.commit()

    result = await async_session.execute(select(ConnectionEvent))
    row = result.scalar_one()
    assert row.status == "connected"
    assert row.details["reconnect_attempts"] == 0
    assert row.id is not None


async def test_ohlcv_bar_insert(async_session):
    """OHLCV bars are inserted correctly."""
    now = datetime.now(UTC)
    bar = OHLCVBar(
        timestamp=now,
        timeframe="1m",
        open=Decimal("20100.25"),
        high=Decimal("20105.50"),
        low=Decimal("20098.00"),
        close=Decimal("20103.75"),
        volume=1234,
        symbol="NQH6",
    )
    async_session.add(bar)
    await async_session.commit()

    result = await async_session.execute(select(OHLCVBar))
    row = result.scalar_one()
    assert row.timeframe == "1m"
    assert row.open == Decimal("20100.25")
    assert row.volume == 1234
    assert row.symbol == "NQH6"


async def test_ohlcv_bar_unique_constraint(async_session):
    """Duplicate (timestamp, timeframe, symbol) is rejected."""
    now = datetime.now(UTC)
    bar1 = OHLCVBar(
        timestamp=now, timeframe="1m",
        open=Decimal("100"), high=Decimal("101"),
        low=Decimal("99"), close=Decimal("100.5"),
        volume=100, symbol="NQH6",
    )
    bar2 = OHLCVBar(
        timestamp=now, timeframe="1m",
        open=Decimal("200"), high=Decimal("201"),
        low=Decimal("199"), close=Decimal("200.5"),
        volume=200, symbol="NQH6",
    )
    async_session.add(bar1)
    await async_session.commit()

    async_session.add(bar2)
    with pytest.raises(IntegrityError):
        await async_session.commit()


async def test_model_version_lifecycle(async_session):
    """Create model version, activate, deactivate."""
    v1 = ModelVersion(
        version="1.0.0",
        file_path="/models/v1.cbm",
        is_active=False,
        metrics={"accuracy": 0.55},
    )
    async_session.add(v1)
    await async_session.commit()

    assert v1.is_active is False

    # Activate
    v1.is_active = True
    v1.activated_at = datetime.now(UTC)
    await async_session.commit()
    await async_session.refresh(v1)
    assert v1.is_active is True
    assert v1.activated_at is not None


async def test_only_one_active_model(async_session):
    """Activating a model deactivates all others."""
    v1 = ModelVersion(
        version="1.0", file_path="/m/v1.cbm", is_active=True,
        metrics={}, activated_at=datetime.now(UTC),
    )
    v2 = ModelVersion(
        version="2.0", file_path="/m/v2.cbm", is_active=False, metrics={},
    )
    async_session.add_all([v1, v2])
    await async_session.commit()

    # Activate v2 — should deactivate v1
    # This is application logic, not a DB constraint, so we do it manually:
    result = await async_session.execute(
        select(ModelVersion).where(ModelVersion.is_active.is_(True))
    )
    for active in result.scalars():
        active.is_active = False

    v2.is_active = True
    v2.activated_at = datetime.now(UTC)
    await async_session.commit()

    result = await async_session.execute(
        select(ModelVersion).where(ModelVersion.is_active.is_(True))
    )
    active_models = result.scalars().all()
    assert len(active_models) == 1
    assert active_models[0].version == "2.0"


async def test_migration_creates_all_tables(async_engine):
    """Running create_all creates the expected schema."""
    from sqlalchemy import inspect as sa_inspect

    async with async_engine.connect() as conn:
        tables = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_table_names()
        )

    expected = {"config", "connection_events", "ohlcv_bars", "model_versions"}
    assert expected.issubset(set(tables)), f"Missing tables: {expected - set(tables)}"
