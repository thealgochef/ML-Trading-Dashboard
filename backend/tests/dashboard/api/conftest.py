"""
Shared test fixtures for Phase 5 — API Server.

Provides a DashboardState with real in-memory managers (no DB, no Rithmic),
a FastAPI test app, and HTTP/WebSocket clients for testing.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from alpha_lab.dashboard.api.server import DashboardState, create_app
from alpha_lab.dashboard.trading.account_manager import AccountManager
from alpha_lab.dashboard.trading.position_monitor import PositionMonitor
from alpha_lab.dashboard.trading.trade_executor import TradeExecutor


@pytest.fixture
def app_state() -> DashboardState:
    """Create a DashboardState with real in-memory managers.

    Pre-populates 2 accounts: A1 (Group A) and B1 (Group B).
    """
    mgr = AccountManager()
    mgr.add_account("A1", Decimal("147"), Decimal("85"), "A")
    mgr.add_account("B1", Decimal("147"), Decimal("85"), "B")

    executor = TradeExecutor(mgr)
    monitor = PositionMonitor(mgr, executor)

    return DashboardState(
        account_manager=mgr,
        trade_executor=executor,
        position_monitor=monitor,
    )


@pytest.fixture
def test_app(app_state: DashboardState):
    """Create a FastAPI app with the test state."""
    return create_app(state=app_state)


@pytest.fixture
def async_client(test_app):
    """Async HTTP client for REST endpoint testing."""
    transport = httpx.ASGITransport(app=test_app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def sync_client(test_app):
    """Sync client for WebSocket testing."""
    from starlette.testclient import TestClient

    return TestClient(test_app)
