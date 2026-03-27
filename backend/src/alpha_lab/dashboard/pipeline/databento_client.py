"""
Databento live client — drop-in alternative to RithmicClient.

Uses the Databento Live API to stream trades and BBO for NQ futures.
Produces the same TradeUpdate / BBOUpdate dataclasses so PipelineService
doesn't need to know which provider is active.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import databento as db

from alpha_lab.dashboard.config.settings import DashboardSettings
from alpha_lab.dashboard.pipeline.price_buffer import OHLCVBar
from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    TradeUpdate,
)

logger = logging.getLogger(__name__)

# Databento Side → aggressor label
_SIDE_MAP = {
    "A": "BUY",   # trade hit the ask → buyer aggressed
    "B": "SELL",   # trade hit the bid → seller aggressed
}


class DatabentoClient:
    """Wraps databento.Live with the same interface as RithmicClient."""

    def __init__(self, settings: DashboardSettings) -> None:
        self._settings = settings
        self._live: db.Live | None = None
        self._status = ConnectionStatus.DISCONNECTED
        self._subscribed_symbol: str = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_task: asyncio.Task | None = None

        self._trade_callbacks: list[Callable[[TradeUpdate], None]] = []
        self._bbo_callbacks: list[Callable[[BBOUpdate], None]] = []
        self._status_callbacks: list[Callable[[ConnectionStatus], None]] = []

    # ── Public interface (mirrors RithmicClient) ──────────────

    async def connect(self) -> None:
        """Create the Databento Live client (connection happens on subscribe)."""
        self._loop = asyncio.get_running_loop()
        self._set_status(ConnectionStatus.CONNECTING)

        api_key = self._settings.databento_api_key
        if api_key is None:
            self._set_status(ConnectionStatus.ERROR)
            raise ValueError("DASHBOARD_DATABENTO_API_KEY not set in .env")

        try:
            self._live = db.Live(
                key=api_key.get_secret_value(),
                reconnect_policy="reconnect",
            )
        except Exception:
            self._set_status(ConnectionStatus.ERROR)
            raise

    async def disconnect(self) -> None:
        """Stop streaming and clean up."""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass

        self._set_status(ConnectionStatus.DISCONNECTED)

    async def subscribe_market_data(self, symbol: str) -> None:
        """Subscribe to trades + BBO for the given continuous symbol."""
        if self._live is None:
            raise RuntimeError("Must call connect() before subscribe_market_data()")

        self._subscribed_symbol = symbol

        # Trades for TradeUpdate
        self._live.subscribe(
            dataset="GLBX.MDP3",
            schema="trades",
            symbols=symbol,
            stype_in="continuous",
        )
        # Top-of-book for BBOUpdate
        self._live.subscribe(
            dataset="GLBX.MDP3",
            schema="mbp-1",
            symbols=symbol,
            stype_in="continuous",
        )

        # Start consuming in a background thread
        self._worker_task = asyncio.create_task(self._consume())

    async def get_front_month_contract(self) -> str | None:
        """Return the continuous front-month symbol for Databento."""
        return f"{self._settings.symbol}.c.0"

    def on_trade(self, callback: Callable[[TradeUpdate], None]) -> None:
        self._trade_callbacks.append(callback)

    def on_bbo(self, callback: Callable[[BBOUpdate], None]) -> None:
        self._bbo_callbacks.append(callback)

    def on_connection_status(self, callback: Callable[[ConnectionStatus], None]) -> None:
        self._status_callbacks.append(callback)

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.CONNECTED

    @property
    def connection_status(self) -> ConnectionStatus:
        return self._status

    # ── Historical backfill ──────────────────────────────────

    async def fetch_historical_ohlcv(
        self, symbol: str, days: int = 7
    ) -> list[OHLCVBar]:
        """Fetch historical 1m OHLCV bars using the Databento Historical API.

        Runs the sync Historical client in a background thread.
        Returns OHLCVBar objects ready for PriceBuffer.load_historical().
        """
        api_key = self._settings.databento_api_key
        if api_key is None:
            logger.warning("No Databento API key — skipping historical backfill")
            return []

        try:
            bars = await asyncio.to_thread(
                self._fetch_historical_sync,
                api_key.get_secret_value(),
                symbol,
                days,
            )
            logger.info("Fetched %d historical 1m bars (%d days)", len(bars), days)
            return bars
        except Exception:
            logger.exception("Historical OHLCV backfill failed — continuing without")
            return []

    @staticmethod
    def _fetch_historical_sync(
        api_key: str, symbol: str, days: int
    ) -> list[OHLCVBar]:
        """Sync helper that calls db.Historical (runs in a thread)."""
        now = datetime.now(timezone.utc)
        # Databento historical data has an availability lag and a midnight-UTC
        # boundary. Use a 2-hour buffer to safely avoid the
        # data_end_after_available_end error.
        end = now - timedelta(hours=2)
        start = now - timedelta(days=days)

        hist = db.Historical(key=api_key)
        data = hist.timeseries.get_range(
            dataset="GLBX.MDP3",
            schema="ohlcv-1m",
            symbols=symbol,
            stype_in="continuous",
            start=start.isoformat(),
            end=end.isoformat(),
        )
        df = data.to_df()

        if df.empty:
            return []

        bars: list[OHLCVBar] = []
        for _, row in df.iterrows():
            # ts_event is the bar open timestamp
            ts = row.get("ts_event") or row.name
            if hasattr(ts, "to_pydatetime"):
                ts = ts.to_pydatetime()
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            bars.append(OHLCVBar(
                timestamp=ts,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
            ))

        return bars

    # ── Historical trades (for tick bar preload) ──────────────

    async def fetch_historical_trades(
        self, symbol: str, days: int = 4
    ) -> "pd.DataFrame":
        """Fetch historical raw trades for tick-bar construction.

        Returns a DataFrame with columns: ts_event, price, size.
        Runs the sync Historical client in a background thread.
        """
        import pandas as pd

        api_key = self._settings.databento_api_key
        if api_key is None:
            logger.warning("No Databento API key — skipping historical trades fetch")
            return pd.DataFrame()

        try:
            df = await asyncio.to_thread(
                self._fetch_trades_sync,
                api_key.get_secret_value(),
                symbol,
                days,
            )
            logger.info(
                "Fetched %d historical trades (%d days) for tick bar preload",
                len(df), days,
            )
            return df
        except Exception:
            logger.exception("Historical trades fetch failed")
            return pd.DataFrame()

    @staticmethod
    def _fetch_trades_sync(
        api_key: str, symbol: str, days: int
    ) -> "pd.DataFrame":
        """Sync helper — fetch raw trades from Databento Historical API."""
        import pandas as pd

        now = datetime.now(timezone.utc)
        end = now - timedelta(hours=2)
        start = now - timedelta(days=days)

        hist = db.Historical(key=api_key)
        data = hist.timeseries.get_range(
            dataset="GLBX.MDP3",
            schema="trades",
            symbols=symbol,
            stype_in="continuous",
            start=start.isoformat(),
            end=end.isoformat(),
        )
        df = data.to_df()
        if df.empty:
            return pd.DataFrame(columns=["ts_event", "price", "size"])

        # Normalize: keep just what we need for tick bar construction
        result = pd.DataFrame({
            "ts_event": df["ts_event"] if "ts_event" in df.columns else df.index,
            "price": df["price"].astype(float),
            "size": df["size"].astype(int),
        })
        return result.sort_values("ts_event").reset_index(drop=True)

    # ── Internal ──────────────────────────────────────────────

    def _set_status(self, status: ConnectionStatus) -> None:
        self._status = status
        for cb in self._status_callbacks:
            cb(status)

    def _set_status_threadsafe(self, status: ConnectionStatus) -> None:
        """Set status from the background thread via the event loop."""
        self._status = status
        if self._loop and self._loop.is_running():
            for cb in self._status_callbacks:
                self._loop.call_soon_threadsafe(cb, status)
        else:
            for cb in self._status_callbacks:
                cb(status)

    async def _consume(self) -> None:
        """Run the blocking Databento iterator in a thread."""
        try:
            self._set_status(ConnectionStatus.CONNECTED)
            logger.info("Databento live stream started for %s", self._subscribed_symbol)
            await asyncio.to_thread(self._blocking_consume)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Databento stream error")
            self._set_status_threadsafe(ConnectionStatus.ERROR)

    def _blocking_consume(self) -> None:
        """Iterate over Databento records (runs in a background thread)."""
        symbol = self._subscribed_symbol or self._settings.symbol

        for record in self._live:
            if isinstance(record, db.TradeMsg):
                ts = record.pretty_ts_event
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                side_str = str(record.side) if record.side else ""
                trade = TradeUpdate(
                    timestamp=ts,
                    price=Decimal(str(record.pretty_price)),
                    size=record.size,
                    aggressor_side=_SIDE_MAP.get(side_str, "UNKNOWN"),
                    symbol=symbol,
                )
                for cb in self._trade_callbacks:
                    cb(trade)

            elif isinstance(record, db.MBP1Msg):
                level = record.levels[0]
                # Skip if bid or ask is missing/zero
                if level.bid_sz <= 0 or level.ask_sz <= 0:
                    continue

                ts = record.pretty_ts_event
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                bbo = BBOUpdate(
                    timestamp=ts,
                    bid_price=Decimal(str(level.pretty_bid_px)),
                    bid_size=level.bid_sz,
                    ask_price=Decimal(str(level.pretty_ask_px)),
                    ask_size=level.ask_sz,
                    symbol=symbol,
                )
                for cb in self._bbo_callbacks:
                    cb(bbo)

            elif isinstance(record, db.ErrorMsg):
                logger.error("Databento error: %s", record.err)

        # Iterator ended — stream closed
        logger.warning("Databento live stream ended")
        self._set_status_threadsafe(ConnectionStatus.RECONNECTING)
