"""
Phase 5 — Data Routes Tests

Tests historical data query endpoints: trade history, predictions,
performance stats, and equity curves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from alpha_lab.dashboard.pipeline.price_buffer import OHLCVBar
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate
from alpha_lab.dashboard.pipeline.tick_bar_builder import TickBarBuilder


@pytest.mark.asyncio
async def test_get_trades_history(async_client, app_state):
    """GET /api/data/trades returns today's trades from state."""
    # Populate some trade data
    app_state.todays_trades.append({
        "account_id": "APEX-001",
        "direction": "long",
        "pnl": 300.0,
        "exit_reason": "tp",
    })
    app_state.todays_trades.append({
        "account_id": "APEX-002",
        "direction": "long",
        "pnl": -200.0,
        "exit_reason": "sl",
    })

    resp = await async_client.get("/api/data/trades")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["trades"]) == 2
    assert data["trades"][0]["pnl"] == 300.0


@pytest.mark.asyncio
async def test_get_predictions_history(async_client, app_state):
    """GET /api/data/predictions returns today's predictions."""
    app_state.todays_predictions.append({
        "event_id": "evt_1",
        "predicted_class": "tradeable_reversal",
        "is_executable": True,
        "prediction_correct": True,
    })

    resp = await async_client.get("/api/data/predictions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["predictions"]) == 1
    assert data["predictions"][0]["predicted_class"] == "tradeable_reversal"


@pytest.mark.asyncio
async def test_get_performance_stats(async_client, app_state):
    """GET /api/data/performance computes win rate and accuracy."""
    app_state.todays_trades.extend([
        {"pnl": 300.0}, {"pnl": 200.0}, {"pnl": -100.0},
    ])
    app_state.todays_predictions.extend([
        {"prediction_correct": True},
        {"prediction_correct": True},
        {"prediction_correct": False},
    ])

    resp = await async_client.get("/api/data/performance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_trades"] == 3
    assert data["wins"] == 2
    assert data["losses"] == 1
    assert data["total_pnl"] == 400.0
    assert data["win_rate"] == pytest.approx(0.6667, abs=0.001)
    assert data["prediction_accuracy"] == pytest.approx(0.6667, abs=0.001)


@pytest.mark.asyncio
async def test_get_equity_curve(async_client, app_state):
    """GET /api/data/equity-curve returns account balance snapshots."""
    resp = await async_client.get("/api/data/equity-curve")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["snapshots"]) == 2  # Two pre-created accounts
    assert data["snapshots"][0]["balance"] == 50000.0

    # Filter by account_id
    acct_id = app_state.account_manager.get_all_accounts()[0].account_id
    resp2 = await async_client.get(f"/api/data/equity-curve?account_id={acct_id}")
    assert len(resp2.json()["snapshots"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_tick_timeframe_includes_partial_from_builder(async_client, app_state):
    """GET /api/data/ohlcv uses TickBarBuilder include_partial for tick frames."""
    builder = TickBarBuilder(tick_counts=[987])
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # preload one completed bar from historical backfill path
    builder.preload_historical("987t", [
        OHLCVBar(
            timestamp=base,
            open=Decimal("100.0"),
            high=Decimal("100.0"),
            low=Decimal("100.0"),
            close=Decimal("100.0"),
            volume=10,
        )
    ])

    # add in-progress partial via trade flow
    for i, price in enumerate([102.0, 103.0], start=1):
        builder.on_trade(TradeUpdate(
            timestamp=base.replace(second=base.second + i),
            price=Decimal(str(price)),
            size=1,
            aggressor_side='BUY',
            symbol='NQH6',
        ))

    # pipeline is only checked for non-None for this route branch
    app_state.pipeline = SimpleNamespace(_buffer=None)
    app_state.tick_bar_builder = builder

    resp = await async_client.get('/api/data/ohlcv?timeframe=987t')
    assert resp.status_code == 200
    bars = resp.json()['bars']

    assert len(bars) == 2
    assert bars[0]['open'] == 100.0
    assert bars[0]['close'] == 100.0
    assert bars[1]['open'] == 102.0
    assert bars[1]['close'] == 103.0
