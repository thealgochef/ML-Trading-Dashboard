"""
Phase 1 — Pipeline Service Tests

Tests the main orchestrator that starts and coordinates all pipeline
components. The pipeline service is the entry point for the entire
data pipeline — it wires the Rithmic client to the tick recorder
and price buffer, and provides the interface for later phases to
register additional data consumers.

Business context: The pipeline runs 24/7 independently of the dashboard.
If the dashboard is closed, the pipeline continues streaming and recording.
When the dashboard reconnects, it reads current state from PostgreSQL and
the price buffer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alpha_lab.dashboard.config.settings import DashboardSettings
from alpha_lab.dashboard.pipeline.pipeline_service import PipelineService
from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    TradeUpdate,
)

pytestmark = pytest.mark.asyncio


def _make_settings(**overrides) -> DashboardSettings:
    defaults = dict(
        rithmic_username="test_user",
        rithmic_password="test_pass",
        rithmic_system="Rithmic Test",
        rithmic_url="wss://test.rithmic.com:443",
        database_url="sqlite+aiosqlite://",
    )
    defaults.update(overrides)
    return DashboardSettings(**defaults)


def _mock_trade() -> TradeUpdate:
    return TradeUpdate(
        timestamp=datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC),
        price=Decimal("20100.25"),
        size=3,
        aggressor_side="BUY",
        symbol="NQH6",
    )


def _mock_bbo() -> BBOUpdate:
    return BBOUpdate(
        timestamp=datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC),
        bid_price=Decimal("20100.00"),
        bid_size=15,
        ask_price=Decimal("20100.25"),
        ask_size=12,
        symbol="NQH6",
    )


@pytest.fixture
def mock_rithmic_client():
    """Mock RithmicClient."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.subscribe_market_data = AsyncMock()
    client.get_front_month_contract = AsyncMock(return_value="NQH6")
    client.connection_status = ConnectionStatus.DISCONNECTED

    # Store callbacks registered via on_trade/on_bbo/on_connection_status
    client._trade_cbs: list = []
    client._bbo_cbs: list = []
    client._status_cbs: list = []

    def on_trade(cb):
        client._trade_cbs.append(cb)

    def on_bbo(cb):
        client._bbo_cbs.append(cb)

    def on_connection_status(cb):
        client._status_cbs.append(cb)

    client.on_trade.side_effect = on_trade
    client.on_bbo.side_effect = on_bbo
    client.on_connection_status.side_effect = on_connection_status

    return client


@pytest.fixture
def mock_recorder():
    """Mock TickRecorder."""
    recorder = MagicMock()
    recorder.record_trade = MagicMock()
    recorder.record_bbo = MagicMock()
    recorder.flush = MagicMock()
    recorder.close = MagicMock()
    return recorder


@pytest.fixture
def mock_buffer():
    """Mock PriceBuffer."""
    buf = MagicMock()
    buf.add_trade = MagicMock()
    buf.add_bbo = MagicMock()
    return buf


@pytest.fixture
def service(mock_rithmic_client, mock_recorder, mock_buffer):
    """Create a PipelineService with all mocked components."""
    settings = _make_settings()
    with patch(
        "alpha_lab.dashboard.pipeline.pipeline_service.TickRecorder",
        return_value=mock_recorder,
    ), patch(
        "alpha_lab.dashboard.pipeline.pipeline_service.PriceBuffer",
        return_value=mock_buffer,
    ), patch(
        "alpha_lab.dashboard.pipeline.pipeline_service.init_db",
        new_callable=AsyncMock,
    ), patch(
        "alpha_lab.dashboard.pipeline.pipeline_service.create_db_engine",
        return_value=MagicMock(dispose=AsyncMock()),
    ):
        svc = PipelineService(settings, client=mock_rithmic_client)
    return svc


async def test_start_initializes_all_components(service, mock_rithmic_client):
    """start() creates and connects Rithmic client, tick recorder, and price buffer."""
    await service.start()

    mock_rithmic_client.connect.assert_awaited_once()
    mock_rithmic_client.get_front_month_contract.assert_awaited_once()
    mock_rithmic_client.subscribe_market_data.assert_awaited_once_with("NQH6")
    assert service.is_running


async def test_stop_shuts_down_cleanly(service, mock_rithmic_client, mock_recorder):
    """stop() flushes recorder, disconnects client, closes DB."""
    await service.start()
    await service.stop()

    mock_recorder.close.assert_called_once()
    mock_rithmic_client.disconnect.assert_awaited_once()
    assert not service.is_running


async def test_trade_fanout(service, mock_rithmic_client, mock_recorder, mock_buffer):
    """Incoming trades are delivered to recorder, price buffer, and registered handlers."""
    received: list[TradeUpdate] = []
    service.register_trade_handler(lambda t: received.append(t))
    service._record_ticks = True  # Enable recording (suppressed when client is injected)

    await service.start()

    # Simulate a trade arriving
    trade = _mock_trade()
    for cb in mock_rithmic_client._trade_cbs:
        cb(trade)

    mock_recorder.record_trade.assert_called_once_with(trade)
    mock_buffer.add_trade.assert_called_once_with(trade)
    assert len(received) == 1
    assert received[0] is trade


async def test_bbo_fanout(service, mock_rithmic_client, mock_recorder, mock_buffer):
    """Incoming BBO updates are delivered to all consumers."""
    received: list[BBOUpdate] = []
    service.register_bbo_handler(lambda b: received.append(b))
    service._record_ticks = True  # Enable recording (suppressed when client is injected)

    await service.start()

    bbo = _mock_bbo()
    for cb in mock_rithmic_client._bbo_cbs:
        cb(bbo)

    mock_recorder.record_bbo.assert_called_once_with(bbo)
    mock_buffer.add_bbo.assert_called_once_with(bbo)
    assert len(received) == 1


async def test_connection_status_stored(service, mock_rithmic_client):
    """Connection status changes are tracked."""
    await service.start()

    # Simulate status change callback
    for cb in mock_rithmic_client._status_cbs:
        cb(ConnectionStatus.RECONNECTING)

    assert service.connection_status == ConnectionStatus.RECONNECTING


async def test_register_additional_handler(service, mock_rithmic_client):
    """register_trade_handler() adds a consumer that receives subsequent trades."""
    handler_a: list = []
    handler_b: list = []

    service.register_trade_handler(lambda t: handler_a.append(t))
    await service.start()
    service.register_trade_handler(lambda t: handler_b.append(t))

    trade = _mock_trade()
    for cb in mock_rithmic_client._trade_cbs:
        cb(trade)

    assert len(handler_a) == 1
    assert len(handler_b) == 1


async def test_service_lifecycle(service, mock_rithmic_client, mock_recorder, mock_buffer):
    """Start -> receive data -> stop -> verify all data persisted."""
    await service.start()
    assert service.is_running

    trade = _mock_trade()
    for cb in mock_rithmic_client._trade_cbs:
        cb(trade)

    await service.stop()

    assert not service.is_running
    mock_recorder.close.assert_called_once()
    mock_rithmic_client.disconnect.assert_awaited_once()


async def test_handles_rithmic_disconnect(service, mock_rithmic_client, mock_recorder, mock_buffer):
    """When Rithmic disconnects, service continues running."""
    await service.start()

    # Simulate disconnect
    for cb in mock_rithmic_client._status_cbs:
        cb(ConnectionStatus.RECONNECTING)

    assert service.is_running
    assert service.connection_status == ConnectionStatus.RECONNECTING


async def test_is_running_property(service):
    """Correctly reflects whether the service is active."""
    assert not service.is_running

    await service.start()
    assert service.is_running

    await service.stop()
    assert not service.is_running
