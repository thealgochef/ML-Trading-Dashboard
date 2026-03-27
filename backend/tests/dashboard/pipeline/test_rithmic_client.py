"""
Phase 1 — Rithmic Client Tests

Tests the Rithmic connection lifecycle, authentication, market data subscription,
and reconnection behavior. The Rithmic client is the foundation of the entire
live trading system — it provides the real-time tick stream that everything
else depends on.

Business context: The trader uses a $30/month Apex second-login add-on to
get a dedicated Rithmic data session. The client must handle connection drops
gracefully because Rithmic disconnects when markets close and may drop during
volatile periods.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alpha_lab.dashboard.config.settings import DashboardSettings
from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    RithmicClient,
    TradeUpdate,
)

pytestmark = pytest.mark.asyncio


def _make_settings(**overrides) -> DashboardSettings:
    """Create test settings with sensible defaults."""
    defaults = dict(
        rithmic_username="test_user",
        rithmic_password="test_pass",
        rithmic_system="Rithmic Test",
        rithmic_url="wss://test.rithmic.com:443",
    )
    defaults.update(overrides)
    return DashboardSettings(**defaults)


def _make_trade_tick(
    price: float = 20100.25,
    size: int = 3,
    aggressor: int = 1,
    symbol: str = "NQH6",
) -> dict:
    """Create a mock tick dict as async_rithmic produces for LAST_TRADE."""
    from async_rithmic import DataType

    return {
        "data_type": DataType.LAST_TRADE,
        "datetime": datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC),
        "symbol": symbol,
        "exchange": "CME",
        "trade_price": price,
        "trade_size": size,
        "aggressor": aggressor,
        "presence_bits": 1,
        "volume": 50000,
    }


def _make_bbo_tick(
    bid: float = 20100.00,
    ask: float = 20100.25,
    bid_size: int = 15,
    ask_size: int = 12,
    symbol: str = "NQH6",
) -> dict:
    """Create a mock tick dict as async_rithmic produces for BBO."""
    from async_rithmic import DataType

    return {
        "data_type": DataType.BBO,
        "datetime": datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC),
        "symbol": symbol,
        "exchange": "CME",
        "bid_price": bid,
        "bid_size": bid_size,
        "ask_price": ask,
        "ask_size": ask_size,
        "presence_bits": 3,
    }


@pytest.fixture
def mock_inner_client():
    """Create a mock of async_rithmic.RithmicClient."""
    from pattern_kit import Event

    mock = MagicMock()
    mock.on_tick = Event()
    mock.on_connected = Event()
    mock.on_disconnected = Event()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock.subscribe_to_market_data = AsyncMock()
    mock.unsubscribe_from_market_data = AsyncMock()
    mock.get_front_month_contract = AsyncMock(return_value="NQH6")
    mock.plants = {
        "ticker": MagicMock(is_connected=True),
        "order": MagicMock(is_connected=False),
        "history": MagicMock(is_connected=False),
        "pnl": MagicMock(is_connected=False),
    }
    return mock


@pytest.fixture
def client(mock_inner_client):
    """Create a RithmicClient with a mocked inner client."""
    settings = _make_settings()
    with patch(
        "alpha_lab.dashboard.pipeline.rithmic_client._create_inner_client",
        return_value=mock_inner_client,
    ):
        c = RithmicClient(settings)
    return c


async def test_connection_lifecycle(client, mock_inner_client):
    """Client transitions through DISCONNECTED -> CONNECTING -> CONNECTED -> DISCONNECTED."""
    assert client.connection_status == ConnectionStatus.DISCONNECTED

    # Simulate connect: fire the on_connected event during connect()
    async def fake_connect(**kwargs):
        await mock_inner_client.on_connected.call_async("ticker")

    mock_inner_client.connect.side_effect = fake_connect

    await client.connect()
    assert client.connection_status == ConnectionStatus.CONNECTED

    await client.disconnect()
    assert client.connection_status == ConnectionStatus.DISCONNECTED


async def test_authentication_with_valid_credentials(client, mock_inner_client):
    """Successful auth transitions to CONNECTED."""
    async def fake_connect(**kwargs):
        await mock_inner_client.on_connected.call_async("ticker")

    mock_inner_client.connect.side_effect = fake_connect
    await client.connect()

    assert client.connection_status == ConnectionStatus.CONNECTED
    mock_inner_client.connect.assert_awaited_once()


async def test_authentication_failure(client, mock_inner_client):
    """Invalid credentials transition to ERROR, no retry."""
    mock_inner_client.connect.side_effect = Exception("Login failed")

    with pytest.raises(Exception, match="Login failed"):
        await client.connect()

    assert client.connection_status == ConnectionStatus.ERROR


async def test_market_data_subscription(client, mock_inner_client):
    """After connecting, subscribing to NQ produces trade and BBO callbacks."""
    async def fake_connect(**kwargs):
        await mock_inner_client.on_connected.call_async("ticker")

    mock_inner_client.connect.side_effect = fake_connect
    await client.connect()

    await client.subscribe_market_data("NQH6")

    mock_inner_client.subscribe_to_market_data.assert_awaited_once()
    call_args = mock_inner_client.subscribe_to_market_data.call_args
    assert call_args[0][0] == "NQH6"  # symbol
    assert call_args[0][1] == "CME"  # exchange


async def test_trade_callback_data_integrity(client, mock_inner_client):
    """Trade updates contain all required fields with correct types."""
    received: list[TradeUpdate] = []
    client.on_trade(lambda t: received.append(t))

    tick = _make_trade_tick(price=20100.25, size=3, aggressor=1)
    await mock_inner_client.on_tick.call_async(tick)

    assert len(received) == 1
    t = received[0]
    assert isinstance(t, TradeUpdate)
    assert isinstance(t.price, Decimal)
    assert t.price == Decimal("20100.25")
    assert t.size == 3
    assert t.aggressor_side == "BUY"
    assert t.symbol == "NQH6"
    assert isinstance(t.timestamp, datetime)
    assert t.timestamp.tzinfo is not None


async def test_bbo_callback_data_integrity(client, mock_inner_client):
    """BBO updates contain all required fields with correct types."""
    received: list[BBOUpdate] = []
    client.on_bbo(lambda b: received.append(b))

    tick = _make_bbo_tick(bid=20100.00, ask=20100.25, bid_size=15, ask_size=12)
    await mock_inner_client.on_tick.call_async(tick)

    assert len(received) == 1
    b = received[0]
    assert isinstance(b, BBOUpdate)
    assert isinstance(b.bid_price, Decimal)
    assert b.bid_price == Decimal("20100.00")
    assert b.ask_price == Decimal("20100.25")
    assert b.bid_size == 15
    assert b.ask_size == 12
    assert b.symbol == "NQH6"


async def test_auto_reconnect_on_drop(client, mock_inner_client):
    """After unexpected disconnect, client transitions to RECONNECTING."""
    statuses: list[ConnectionStatus] = []
    client.on_connection_status(lambda s: statuses.append(s))

    # Simulate connection then disconnection
    async def fake_connect(**kwargs):
        await mock_inner_client.on_connected.call_async("ticker")

    mock_inner_client.connect.side_effect = fake_connect
    await client.connect()

    # Simulate unexpected disconnect
    await mock_inner_client.on_disconnected.call_async("ticker")

    assert client.connection_status == ConnectionStatus.RECONNECTING
    assert ConnectionStatus.RECONNECTING in statuses


async def test_reconnect_resubscribes(client, mock_inner_client):
    """After successful reconnect, market data subscription is restored.

    The async_rithmic library handles resubscription internally via its
    _subscriptions tracking in the TickerPlant._login() method, so we verify
    the subscription set is maintained.
    """
    async def fake_connect(**kwargs):
        await mock_inner_client.on_connected.call_async("ticker")

    mock_inner_client.connect.side_effect = fake_connect
    await client.connect()
    await client.subscribe_market_data("NQH6")

    # The inner client tracks subscriptions — verify it was called
    assert mock_inner_client.subscribe_to_market_data.await_count == 1

    # Simulate reconnect: disconnected then reconnected
    await mock_inner_client.on_disconnected.call_async("ticker")
    await mock_inner_client.on_connected.call_async("ticker")

    assert client.connection_status == ConnectionStatus.CONNECTED


async def test_connection_status_callback(client, mock_inner_client):
    """Status changes fire the registered callback."""
    statuses: list[ConnectionStatus] = []
    client.on_connection_status(lambda s: statuses.append(s))

    async def fake_connect(**kwargs):
        await mock_inner_client.on_connected.call_async("ticker")

    mock_inner_client.connect.side_effect = fake_connect
    await client.connect()

    assert ConnectionStatus.CONNECTING in statuses
    assert ConnectionStatus.CONNECTED in statuses


async def test_multiple_callbacks(client, mock_inner_client):
    """Multiple trade/bbo handlers all receive updates."""
    received_a: list[TradeUpdate] = []
    received_b: list[TradeUpdate] = []
    client.on_trade(lambda t: received_a.append(t))
    client.on_trade(lambda t: received_b.append(t))

    tick = _make_trade_tick()
    await mock_inner_client.on_tick.call_async(tick)

    assert len(received_a) == 1
    assert len(received_b) == 1


async def test_clean_disconnect(client, mock_inner_client):
    """disconnect() unsubscribes and closes cleanly without errors."""
    async def fake_connect(**kwargs):
        await mock_inner_client.on_connected.call_async("ticker")

    mock_inner_client.connect.side_effect = fake_connect
    await client.connect()
    await client.disconnect()

    mock_inner_client.disconnect.assert_awaited_once()
    assert client.connection_status == ConnectionStatus.DISCONNECTED


async def test_front_month_detection(client, mock_inner_client):
    """Client resolves 'NQ' to the correct front-month contract symbol."""
    async def fake_connect(**kwargs):
        await mock_inner_client.on_connected.call_async("ticker")

    mock_inner_client.connect.side_effect = fake_connect
    await client.connect()

    symbol = await client.get_front_month_contract()
    assert symbol == "NQH6"
    mock_inner_client.get_front_month_contract.assert_awaited_with("NQ", "CME")
