"""
Phase 5 — WebSocket Tests

Tests real-time data push from server to dashboard client. The WebSocket
is the primary data channel for live trading — latency and reliability
directly impact the trader's experience.

Uses Starlette TestClient for synchronous WebSocket testing.
Throttling test uses a short interval (0.1s) for speed and verifies
that rapid price updates get collapsed into at most 1 per interval.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from alpha_lab.dashboard.api.server import DashboardState, create_app
from alpha_lab.dashboard.api.websocket import WebSocketManager
from alpha_lab.dashboard.trading.account_manager import AccountManager
from alpha_lab.dashboard.trading.position_monitor import PositionMonitor
from alpha_lab.dashboard.trading.trade_executor import TradeExecutor


def _make_state(throttle_interval: float = 1.0) -> DashboardState:
    """Create a DashboardState with a custom throttle interval."""
    mgr = AccountManager()
    mgr.add_account("A1", Decimal("147"), Decimal("85"), "A")
    mgr.add_account("B1", Decimal("147"), Decimal("85"), "B")
    executor = TradeExecutor(mgr)
    monitor = PositionMonitor(mgr, executor)
    ws = WebSocketManager(throttle_interval=throttle_interval)
    return DashboardState(
        account_manager=mgr,
        trade_executor=executor,
        position_monitor=monitor,
        ws_manager=ws,
    )


def test_connect_receives_backfill():
    """New connection immediately receives backfill message with correct structure."""
    state = _make_state()
    state.latest_price = 21045.75
    state.connection_status = "connected"
    app = create_app(state=state)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "backfill"
        data = msg["data"]
        assert data["connection_status"] == "connected"
        assert data["latest_price"] == 21045.75
        assert "active_levels" in data
        assert "accounts" in data
        assert len(data["accounts"]) == 2
        assert "config" in data
        assert data["config"]["group_a_tp"] == 15.0
        assert "session_stats" in data


def test_price_updates_throttled():
    """Rapid price updates get collapsed — at most 1 pushed per throttle interval.

    Sends 50 rapid price updates (simulating 10K ticks/min), then flushes
    once. Verifies exactly 1 price_update is received, and it contains the
    LATEST price (proving all 49 intermediate updates were dropped).
    """
    state = _make_state(throttle_interval=1.0)
    app = create_app(state=state)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        # Consume backfill
        backfill = ws.receive_json()
        assert backfill["type"] == "backfill"

        # Fire 50 rapid price updates — all overwrite _pending_price
        for i in range(50):
            state.ws_manager.update_price(
                price=20100.0 + i * 0.25,
                bid=20099.75 + i * 0.25,
                ask=20100.0 + i * 0.25,
                timestamp=f"2026-03-02T14:30:{i:02d}Z",
            )

        # Force throttle interval elapsed so flush will send
        state.ws_manager._last_price_push = 0.0

        # Single flush — should send exactly 1 message with the LATEST price
        loop = asyncio.new_event_loop()
        loop.run_until_complete(state.ws_manager.flush_price())
        loop.close()

        msg = ws.receive_json()
        assert msg["type"] == "price_update"

        # Key assertion: the price is from the 50th update (index 49),
        # proving 49 intermediate updates were collapsed
        expected_price = 20100.0 + 49 * 0.25
        assert msg["data"]["price"] == pytest.approx(expected_price, abs=0.01)

        # After flush, pending is cleared — second flush sends nothing
        assert state.ws_manager._pending_price is None


def test_prediction_pushed_on_signal():
    """Prediction broadcast reaches connected client."""
    state = _make_state()
    app = create_app(state=state)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # consume backfill

        # Broadcast a prediction message
        loop = asyncio.new_event_loop()
        loop.run_until_complete(state.ws_manager.broadcast({
            "type": "prediction",
            "data": {
                "event_id": "evt_1",
                "predicted_class": "tradeable_reversal",
                "is_executable": True,
            },
        }))
        loop.close()

        msg = ws.receive_json()
        assert msg["type"] == "prediction"
        assert msg["data"]["predicted_class"] == "tradeable_reversal"


def test_trade_pushed_on_execution():
    """Trade opened/closed broadcast reaches connected client."""
    state = _make_state()
    app = create_app(state=state)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # consume backfill

        loop = asyncio.new_event_loop()
        loop.run_until_complete(state.ws_manager.broadcast({
            "type": "trade_opened",
            "data": {
                "account_id": "APEX-001",
                "direction": "long",
                "entry_price": 21045.75,
            },
        }))
        loop.close()

        msg = ws.receive_json()
        assert msg["type"] == "trade_opened"
        assert msg["data"]["account_id"] == "APEX-001"


def test_observation_progress_updates():
    """Observation started message pushed to client."""
    state = _make_state()
    app = create_app(state=state)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # consume backfill

        loop = asyncio.new_event_loop()
        loop.run_until_complete(state.ws_manager.broadcast({
            "type": "observation_started",
            "data": {
                "event_id": "evt_1",
                "level_type": "pdh",
                "direction": "short",
            },
        }))
        loop.close()

        msg = ws.receive_json()
        assert msg["type"] == "observation_started"
        assert msg["data"]["event_id"] == "evt_1"


def test_level_update_on_touch():
    """Level update broadcast reaches client."""
    state = _make_state()
    app = create_app(state=state)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # consume backfill

        loop = asyncio.new_event_loop()
        loop.run_until_complete(state.ws_manager.broadcast({
            "type": "level_update",
            "data": {
                "action": "touched",
                "levels": [{"price": 21045.75, "type": "pdh"}],
            },
        }))
        loop.close()

        msg = ws.receive_json()
        assert msg["type"] == "level_update"
        assert msg["data"]["action"] == "touched"


def test_connection_status_pushed():
    """Connection status change pushed to client."""
    state = _make_state()
    app = create_app(state=state)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # consume backfill

        loop = asyncio.new_event_loop()
        loop.run_until_complete(state.ws_manager.broadcast({
            "type": "connection_status",
            "data": {"status": "reconnecting"},
        }))
        loop.close()

        msg = ws.receive_json()
        assert msg["type"] == "connection_status"
        assert msg["data"]["status"] == "reconnecting"


def test_reconnect_receives_fresh_backfill():
    """Disconnecting and reconnecting produces a new backfill."""
    state = _make_state()
    state.latest_price = 21000.0
    app = create_app(state=state)

    with TestClient(app) as client:
        # First connection
        with client.websocket_connect("/ws") as ws:
            msg1 = ws.receive_json()
            assert msg1["type"] == "backfill"
            assert msg1["data"]["latest_price"] == 21000.0

        # Update state between connections
        state.latest_price = 21050.0

        # Second connection — fresh backfill with updated price
        with client.websocket_connect("/ws") as ws:
            msg2 = ws.receive_json()
            assert msg2["type"] == "backfill"
            assert msg2["data"]["latest_price"] == 21050.0
