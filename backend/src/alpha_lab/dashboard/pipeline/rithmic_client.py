"""
Rithmic client wrapper around async_rithmic.

Adapts the async_rithmic event-based API to typed dataclasses and a clean
callback interface for the rest of the pipeline. Handles connection lifecycle,
reconnection status tracking, and front-month contract detection.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from async_rithmic import DataType, ReconnectionSettings, SysInfraType
from async_rithmic import RithmicClient as _InnerRithmicClient

from alpha_lab.dashboard.config.settings import DashboardSettings

logger = logging.getLogger(__name__)

# Rithmic TransactionType enum values (from protobuf)
_AGGRESSOR_MAP = {1: "BUY", 2: "SELL"}


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    """A single trade from the Rithmic stream."""

    timestamp: datetime  # UTC with tzinfo
    price: Decimal
    size: int
    aggressor_side: str  # 'BUY' or 'SELL'
    symbol: str


@dataclass(frozen=True, slots=True)
class BBOUpdate:
    """A best bid/offer update from the Rithmic stream."""

    timestamp: datetime  # UTC with tzinfo
    bid_price: Decimal
    bid_size: int
    ask_price: Decimal
    ask_size: int
    symbol: str


class ConnectionStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


def _create_inner_client(settings: DashboardSettings) -> _InnerRithmicClient:
    """Create the underlying async_rithmic client. Separated for testability."""
    return _InnerRithmicClient(
        user=settings.rithmic_username,
        password=settings.rithmic_password.get_secret_value(),
        system_name=settings.rithmic_system,
        app_name=settings.rithmic_app_name,
        app_version=settings.rithmic_app_version,
        url=settings.rithmic_url,
        reconnection_settings=ReconnectionSettings(
            max_retries=None,
            backoff_type="exponential",
            interval=2,
            max_delay=30,
            jitter_range=(0.5, 2.0),
        ),
    )


class RithmicClient:
    """Wraps async_rithmic.RithmicClient with typed callbacks and status tracking."""

    def __init__(self, settings: DashboardSettings) -> None:
        self._settings = settings
        self._inner = _create_inner_client(settings)
        self._status = ConnectionStatus.DISCONNECTED

        self._trade_callbacks: list[Callable[[TradeUpdate], None]] = []
        self._bbo_callbacks: list[Callable[[BBOUpdate], None]] = []
        self._status_callbacks: list[Callable[[ConnectionStatus], None]] = []

        # Wire inner client events to our handlers
        self._inner.on_tick += self._handle_tick
        self._inner.on_connected += self._handle_connected
        self._inner.on_disconnected += self._handle_disconnected

    # ── Public interface ──────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Rithmic and authenticate. Only connects the ticker plant."""
        self._set_status(ConnectionStatus.CONNECTING)
        try:
            await self._inner.connect(plants=[SysInfraType.TICKER_PLANT])
        except Exception:
            self._set_status(ConnectionStatus.ERROR)
            raise

    async def disconnect(self) -> None:
        """Clean shutdown — unsubscribe and close connection."""
        await self._inner.disconnect()
        self._set_status(ConnectionStatus.DISCONNECTED)

    async def subscribe_market_data(self, symbol: str) -> None:
        """Subscribe to trades + BBO for the given symbol."""
        await self._inner.subscribe_to_market_data(
            symbol, self._settings.exchange, DataType.LAST_TRADE | DataType.BBO
        )

    async def get_front_month_contract(self) -> str | None:
        """Resolve the front-month NQ contract symbol."""
        return await self._inner.get_front_month_contract(
            self._settings.symbol, self._settings.exchange
        )

    def on_trade(self, callback: Callable[[TradeUpdate], None]) -> None:
        """Register a callback for trade updates."""
        self._trade_callbacks.append(callback)

    def on_bbo(self, callback: Callable[[BBOUpdate], None]) -> None:
        """Register a callback for BBO updates."""
        self._bbo_callbacks.append(callback)

    def on_connection_status(self, callback: Callable[[ConnectionStatus], None]) -> None:
        """Register a callback for connection state changes."""
        self._status_callbacks.append(callback)

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.CONNECTED

    @property
    def connection_status(self) -> ConnectionStatus:
        return self._status

    # ── Internal handlers ─────────────────────────────────────────

    def _set_status(self, status: ConnectionStatus) -> None:
        self._status = status
        for cb in self._status_callbacks:
            cb(status)

    async def _handle_tick(self, data: dict) -> None:
        """Route incoming tick dict to typed callbacks."""
        data_type = data.get("data_type")
        if data_type == DataType.LAST_TRADE:
            trade = TradeUpdate(
                timestamp=data["datetime"],
                price=Decimal(str(data["trade_price"])),
                size=data["trade_size"],
                aggressor_side=_AGGRESSOR_MAP.get(data.get("aggressor", 0), "UNKNOWN"),
                symbol=data.get("symbol", ""),
            )
            for cb in self._trade_callbacks:
                cb(trade)

        elif data_type == DataType.BBO:
            bbo = BBOUpdate(
                timestamp=data["datetime"],
                bid_price=Decimal(str(data["bid_price"])),
                bid_size=data["bid_size"],
                ask_price=Decimal(str(data["ask_price"])),
                ask_size=data["ask_size"],
                symbol=data.get("symbol", ""),
            )
            for cb in self._bbo_callbacks:
                cb(bbo)

    async def _handle_connected(self, plant_type: str) -> None:
        """Handle connection established event."""
        if plant_type == "ticker":
            logger.info("Rithmic ticker plant connected")
            self._set_status(ConnectionStatus.CONNECTED)

    async def _handle_disconnected(self, plant_type: str) -> None:
        """Handle disconnection event — transition to RECONNECTING."""
        if plant_type == "ticker":
            logger.warning("Rithmic ticker plant disconnected, will reconnect")
            self._set_status(ConnectionStatus.RECONNECTING)
