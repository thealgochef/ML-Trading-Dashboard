"""
Phase 5 — Data Routes Tests

Tests historical data query endpoints: trade history, predictions,
performance stats, and equity curves.
"""

from __future__ import annotations

import pytest


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
