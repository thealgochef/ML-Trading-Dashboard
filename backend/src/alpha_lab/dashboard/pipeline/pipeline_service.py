"""
Pipeline service — main orchestrator for the data pipeline.

Starts the market data client (Databento or Rithmic), tick recorder,
and price buffer. Routes tick data from the client to all consumers.
Manages the overall service lifecycle.

Later phases will register additional consumers (level engine,
observation engine, paper trading engine) through this service.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from alpha_lab.dashboard.config.settings import DashboardSettings
from alpha_lab.dashboard.db.connection import create_db_engine, init_db
from alpha_lab.dashboard.pipeline.price_buffer import PriceBuffer
from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    TradeUpdate,
)
from alpha_lab.dashboard.pipeline.tick_recorder import TickRecorder

logger = logging.getLogger(__name__)


class PipelineService:
    """Main entry point for the data pipeline.

    Starts the market data client, tick recorder, and price buffer.
    Routes tick data from the client to all consumers.
    """

    def __init__(
        self, settings: DashboardSettings, client: Any | None = None
    ) -> None:
        self._settings = settings
        self._running = False
        self._connection_status = ConnectionStatus.DISCONNECTED

        # Additional handlers registered by later phases
        self._trade_handlers: list[Callable[[TradeUpdate], None]] = []
        self._bbo_handlers: list[Callable[[BBOUpdate], None]] = []
        self._connection_handlers: list[Callable[[ConnectionStatus], None]] = []
        self._backfill_callbacks: list[Callable[[], None]] = []

        # Market data client — injected or created from settings
        if client is not None:
            self._client = client
        else:
            self._client = _create_client(settings)

        # Suppress tick recording when using an injected client (replay mode)
        self._record_ticks = client is None
        self._recorder = TickRecorder(settings.tick_recording_dir)
        self._buffer = PriceBuffer(
            max_duration=timedelta(hours=settings.price_buffer_hours)
        )
        self._engine = create_db_engine(settings.database_url)

    # ── Public interface ──────────────────────────────────────────

    async def start(self) -> None:
        """Start all components and begin streaming."""
        logger.info("Starting pipeline service")

        # Initialize database (graceful — dashboard works without PG)
        try:
            await init_db(self._engine)
        except Exception:
            logger.warning(
                "Database init failed (PostgreSQL not running?) — "
                "continuing without persistence"
            )

        # Wire callbacks before connecting
        self._client.on_trade(self._handle_trade)
        self._client.on_bbo(self._handle_bbo)
        self._client.on_connection_status(self._handle_connection_status)

        # Connect and subscribe
        await self._client.connect()
        symbol = await self._client.get_front_month_contract()
        if symbol:
            await self._client.subscribe_market_data(symbol)
            logger.info("Subscribed to %s", symbol)

            # Backfill historical OHLCV in background (don't block live data)
            asyncio.create_task(self._backfill_historical(symbol))
        else:
            logger.warning("Could not resolve front-month contract")

        self._running = True
        logger.info("Pipeline service started")

    async def stop(self) -> None:
        """Graceful shutdown of all components."""
        logger.info("Stopping pipeline service")

        self._recorder.close()
        await self._client.disconnect()
        try:
            await self._engine.dispose()
        except Exception:
            pass

        self._running = False
        logger.info("Pipeline service stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def connection_status(self) -> ConnectionStatus:
        return self._connection_status

    def register_trade_handler(self, handler: Callable[[TradeUpdate], None]) -> None:
        """Register an additional trade consumer (for later phases)."""
        self._trade_handlers.append(handler)

    def register_bbo_handler(self, handler: Callable[[BBOUpdate], None]) -> None:
        """Register an additional BBO consumer (for later phases)."""
        self._bbo_handlers.append(handler)

    def register_connection_handler(
        self, handler: Callable[[ConnectionStatus], None]
    ) -> None:
        """Register a connection status consumer (for Phase 2 observation manager)."""
        self._connection_handlers.append(handler)

    def register_backfill_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback to run after historical backfill completes."""
        self._backfill_callbacks.append(callback)

    # ── Historical backfill ──────────────────────────────────────

    async def _backfill_historical(self, symbol: str) -> None:
        """Fetch 7 days of historical 1m bars and load into PriceBuffer."""
        if not hasattr(self._client, "fetch_historical_ohlcv"):
            logger.info("Client does not support historical backfill — skipping")
            return

        try:
            bars = await self._client.fetch_historical_ohlcv(symbol, days=7)
            if bars:
                self._buffer.load_historical(bars)
                logger.info("Loaded %d historical bars into price buffer", len(bars))
                # Notify listeners (e.g. LevelEngine) that historical data is ready
                for cb in self._backfill_callbacks:
                    try:
                        cb()
                    except Exception:
                        logger.exception("Backfill callback failed")
            else:
                logger.warning("No historical bars returned")
        except Exception:
            logger.exception("Historical backfill failed — continuing with live only")

    # ── Internal handlers ─────────────────────────────────────────

    def _handle_trade(self, trade: TradeUpdate) -> None:
        """Fan out trade to recorder, buffer, and registered handlers."""
        if self._record_ticks:
            self._recorder.record_trade(trade)
        self._buffer.add_trade(trade)
        for handler in self._trade_handlers:
            try:
                handler(trade)
            except Exception:
                logger.exception("Trade handler error in %s", handler)

    def _handle_bbo(self, bbo: BBOUpdate) -> None:
        """Fan out BBO to recorder, buffer, and registered handlers."""
        if self._record_ticks:
            self._recorder.record_bbo(bbo)
        self._buffer.add_bbo(bbo)
        for handler in self._bbo_handlers:
            try:
                handler(bbo)
            except Exception:
                logger.exception("BBO handler error in %s", handler)

    def _handle_connection_status(self, status: ConnectionStatus) -> None:
        """Track connection status changes and notify registered handlers."""
        logger.info("Connection status: %s -> %s", self._connection_status, status)
        self._connection_status = status
        for handler in self._connection_handlers:
            try:
                handler(status)
            except Exception:
                logger.exception("Connection handler error in %s", handler)


def _create_client(settings: DashboardSettings) -> Any:
    """Create the appropriate market data client based on settings."""
    if settings.data_source == "databento":
        from alpha_lab.dashboard.pipeline.databento_client import DatabentoClient

        return DatabentoClient(settings)

    from alpha_lab.dashboard.pipeline.rithmic_client import RithmicClient

    return RithmicClient(settings)
