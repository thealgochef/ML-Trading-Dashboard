"""
Phase 5 — Trading Routes Tests

Tests the manual trade action endpoints: close-all, close-single, manual-entry.
These endpoints are the trader's primary control interface for managing
positions across simulated Apex accounts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from alpha_lab.dashboard.engine.models import TradeDirection


@pytest.mark.asyncio
async def test_close_all_positions(async_client, app_state):
    """POST /api/trading/close-all closes all open positions."""
    # Open positions on both accounts
    app_state.latest_price = 20100.0
    executor = app_state.trade_executor
    pred = {
        "is_executable": True,
        "trade_direction": TradeDirection.LONG,
        "level_price": Decimal("20100"),
        "event_id": "test",
    }
    executor.on_prediction(pred, datetime(2026, 3, 2, 14, 30, tzinfo=UTC))

    resp = await async_client.post(
        "/api/trading/close-all", json={"reason": "manual"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["closed_trades"]) == 2
    assert all(t["exit_reason"] == "manual" for t in data["closed_trades"])


@pytest.mark.asyncio
async def test_close_single_account(async_client, app_state):
    """POST /api/trading/close/{account_id} closes one account's position."""
    app_state.latest_price = 20100.0
    executor = app_state.trade_executor
    pred = {
        "is_executable": True,
        "trade_direction": TradeDirection.LONG,
        "level_price": Decimal("20100"),
        "event_id": "test",
    }
    executor.on_prediction(pred, datetime(2026, 3, 2, 14, 30, tzinfo=UTC))

    acct_id = app_state.account_manager.get_all_accounts()[0].account_id
    resp = await async_client.post(
        f"/api/trading/close/{acct_id}", json={"reason": "manual"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["closed_trade"]["account_id"] == acct_id
    assert data["closed_trade"]["exit_reason"] == "manual"


@pytest.mark.asyncio
async def test_manual_entry_success(async_client, app_state):
    """POST /api/trading/manual-entry opens positions on all tradeable accounts."""
    app_state.latest_price = 20100.0
    resp = await async_client.post(
        "/api/trading/manual-entry",
        json={"direction": "long"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2  # A1 + B1
    assert all(p["direction"] == "long" for p in data["positions"])


@pytest.mark.asyncio
async def test_manual_entry_no_price(async_client, app_state):
    """POST /api/trading/manual-entry returns 400 when no market price."""
    app_state.latest_price = None
    resp = await async_client.post(
        "/api/trading/manual-entry",
        json={"direction": "long"},
    )
    assert resp.status_code == 400
