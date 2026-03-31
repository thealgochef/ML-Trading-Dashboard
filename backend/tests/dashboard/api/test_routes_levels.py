"""
Phase 5 — Level Routes Tests

Tests manual level management endpoints. Levels are the key price zones
the system monitors for touch events and trade signals.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_lab.dashboard.api.server import DashboardState, create_app
from alpha_lab.dashboard.engine.level_engine import LevelEngine
from alpha_lab.dashboard.engine.models import KeyLevel, LevelSide, LevelType, LevelZone
from alpha_lab.dashboard.pipeline.price_buffer import PriceBuffer
from alpha_lab.dashboard.trading.account_manager import AccountManager
from alpha_lab.dashboard.trading.position_monitor import PositionMonitor
from alpha_lab.dashboard.trading.trade_executor import TradeExecutor


@pytest.fixture
def levels_state() -> DashboardState:
    """DashboardState with a real LevelEngine for level tests."""
    mgr = AccountManager()
    mgr.add_account("A1", Decimal("147"), Decimal("85"), "A")
    executor = TradeExecutor(mgr)
    monitor = PositionMonitor(mgr, executor)
    buffer = PriceBuffer(max_duration=timedelta(hours=48))
    level_engine = LevelEngine(buffer)

    return DashboardState(
        account_manager=mgr,
        trade_executor=executor,
        position_monitor=monitor,
        level_engine=level_engine,
    )


@pytest.fixture
def levels_client(levels_state):
    import httpx

    app = create_app(state=levels_state)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_get_levels(levels_client):
    """GET /api/levels returns zones and manual levels."""
    resp = await levels_client.get("/api/levels")
    assert resp.status_code == 200
    data = resp.json()
    assert "zones" in data
    assert "manual_levels" in data
    assert isinstance(data["zones"], list)


@pytest.mark.asyncio
async def test_get_levels_includes_disabled_fields_consistently(levels_state, levels_client):
    """GET /api/levels includes touched/disabled fields from shared serializer."""
    pdh_level = KeyLevel(
        level_type=LevelType.PDH,
        price=Decimal("21050.0"),
        side=LevelSide.HIGH,
        available_from=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
        source_session_date=date(2026, 3, 2),
    )
    pdl_level = KeyLevel(
        level_type=LevelType.PDL,
        price=Decimal("20950.0"),
        side=LevelSide.LOW,
        available_from=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
        source_session_date=date(2026, 3, 2),
    )
    levels_state.level_engine._zones = [
        LevelZone(
            zone_id="z_touched_pdh",
            representative_price=Decimal("21050.0"),
            levels=[pdh_level],
            side=LevelSide.HIGH,
            is_touched=True,
        ),
        LevelZone(
            zone_id="z_active_pdl",
            representative_price=Decimal("20950.0"),
            levels=[pdl_level],
            side=LevelSide.LOW,
            is_touched=False,
        ),
    ]
    levels_state.disabled_level_types = {LevelType.PDH}

    resp = await levels_client.get("/api/levels")
    assert resp.status_code == 200
    zones = {z["zone_id"]: z for z in resp.json()["zones"]}
    assert zones["z_touched_pdh"]["is_touched"] is True
    assert zones["z_touched_pdh"]["is_disabled"] is True
    assert zones["z_touched_pdh"]["disabled_level_types"] == ["pdh"]
    assert zones["z_active_pdl"]["is_disabled"] is False
    assert zones["z_active_pdl"]["disabled_level_types"] == []


@pytest.mark.asyncio
async def test_add_manual_level(levels_client):
    """POST /api/levels/manual adds a manual level."""
    resp = await levels_client.post("/api/levels/manual", json={"price": 21000.0})
    assert resp.status_code == 200
    level = resp.json()["level"]
    assert level["price"] == 21000.0
    assert level["type"] == "manual"
    assert level["is_manual"] is True

    # Verify it appears in GET
    get_resp = await levels_client.get("/api/levels")
    assert len(get_resp.json()["manual_levels"]) == 1


@pytest.mark.asyncio
async def test_delete_manual_level(levels_client):
    """DELETE /api/levels/manual/{price} removes a manual level."""
    # Add first
    await levels_client.post("/api/levels/manual", json={"price": 21000.0})

    # Delete
    resp = await levels_client.delete("/api/levels/manual/21000.0")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify gone
    get_resp = await levels_client.get("/api/levels")
    assert len(get_resp.json()["manual_levels"]) == 0


@pytest.mark.asyncio
async def test_delete_nonexistent_level(levels_client):
    """DELETE /api/levels/manual/{price} returns 404 for missing level."""
    resp = await levels_client.delete("/api/levels/manual/99999.0")
    assert resp.status_code == 404
