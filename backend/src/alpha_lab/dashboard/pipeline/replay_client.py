"""
Replay client — replays historical Parquet tick data through the pipeline.

Duck-typed to match DatabentoClient's interface so it can be injected
into PipelineService without modifications. Loads MBP-10 Parquet files,
creates TradeUpdate/BBOUpdate objects, and fires callbacks at configurable
speed.

Pre-loads 2 prior dates at max speed (no pausing) so LevelEngine has
PDH/PDL data before the visible replay range begins.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time as time_mod
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pandas as pd

from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    TradeUpdate,
)

logger = logging.getLogger(__name__)

# Number of prior dates to pre-load for LevelEngine PDH/PDL
_PRELOAD_DATES = 2


class ReplayClient:
    """Replays Parquet tick data, duck-typed to DatabentoClient interface.

    Usage::

        client = ReplayClient(data_dir=Path("data/databento/NQ"),
                              start_date="2025-06-05", speed=10.0)
        pipeline = PipelineService(settings, client=client)
        await pipeline.start()
    """

    def __init__(
        self,
        data_dir: Path,
        start_date: str | None = None,
        end_date: str | None = None,
        speed: float = 1.0,
    ) -> None:
        self._data_dir = data_dir

        # Callback lists (same interface as DatabentoClient)
        self._trade_callbacks: list[Callable[[TradeUpdate], None]] = []
        self._bbo_callbacks: list[Callable[[BBOUpdate], None]] = []
        self._status_callbacks: list[Callable[[ConnectionStatus], None]] = []
        self._file_loaded_callbacks: list[Callable[[str], None]] = []

        # Date range
        self._start_date = start_date
        self._end_date = end_date
        self._all_dates: list[str] = []
        self._preload_dates: list[str] = []
        self._visible_dates: list[str] = []

        # Threading control — starts paused in step mode so the user
        # has time to connect the frontend before replay begins.
        self._pause_event = threading.Event()  # set = playing, clear = paused
        self._bar_complete_event = threading.Event()  # set by TickBarBuilder callback
        self._stop_flag = False
        self._speed = speed
        self._step_mode = True
        self._preloading = False

        # Replay thread
        self._thread: threading.Thread | None = None

        # Current replay state (readable by UI)
        self.current_date: str | None = None
        self.current_timestamp: datetime | None = None
        self.replay_complete = False

    # ── DatabentoClient interface (duck-typed) ────────────────────

    def on_trade(self, callback: Callable[[TradeUpdate], None]) -> None:
        self._trade_callbacks.append(callback)

    def on_bbo(self, callback: Callable[[BBOUpdate], None]) -> None:
        self._bbo_callbacks.append(callback)

    def on_connection_status(
        self, callback: Callable[[ConnectionStatus], None],
    ) -> None:
        self._status_callbacks.append(callback)

    def on_file_loaded(self, callback: Callable[[str], None]) -> None:
        """Register callback fired before each date's ticks are replayed."""
        self._file_loaded_callbacks.append(callback)

    async def connect(self) -> None:
        """Discover available dates and compute preload/visible ranges."""
        self._all_dates = self._discover_dates()
        if len(self._all_dates) < 3:
            raise ValueError(
                f"Need >= 3 dates for replay (have {len(self._all_dates)})"
            )

        # Compute visible date range
        if self._start_date:
            try:
                start_idx = next(
                    i for i, d in enumerate(self._all_dates)
                    if d >= self._start_date
                )
            except StopIteration:
                raise ValueError(
                    f"No dates on or after {self._start_date}"
                ) from None
        else:
            start_idx = _PRELOAD_DATES

        if self._end_date:
            try:
                end_idx = next(
                    i for i in range(len(self._all_dates) - 1, -1, -1)
                    if self._all_dates[i] <= self._end_date
                )
            except StopIteration:
                raise ValueError(
                    f"No dates on or before {self._end_date}"
                ) from None
            self._visible_dates = self._all_dates[start_idx:end_idx + 1]
        else:
            self._visible_dates = self._all_dates[start_idx:]

        # Preload dates: up to _PRELOAD_DATES before the first visible date
        preload_start = max(0, start_idx - _PRELOAD_DATES)
        self._preload_dates = self._all_dates[preload_start:start_idx]

        logger.info(
            "Replay: preload %d dates, visible %d dates (%s to %s)",
            len(self._preload_dates),
            len(self._visible_dates),
            self._visible_dates[0] if self._visible_dates else "none",
            self._visible_dates[-1] if self._visible_dates else "none",
        )

    async def disconnect(self) -> None:
        """Stop the replay thread."""
        self._stop_flag = True
        self._pause_event.set()  # Unblock if paused
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    async def subscribe_market_data(self, symbol: str) -> None:
        """Start the replay loop in a background thread.

        The replay thread starts immediately but blocks at the first
        visible tick because _pause_event is initially cleared. The user
        must click Play or Step to advance. Pre-load dates run at max
        speed regardless (pause checks are skipped during preload).
        """
        # Fire connected status
        for cb in self._status_callbacks:
            cb(ConnectionStatus.CONNECTED)

        # Do NOT set _pause_event here — replay starts paused
        self._thread = threading.Thread(
            target=self._replay_loop, daemon=True, name="replay-worker",
        )
        self._thread.start()

    async def get_front_month_contract(self) -> str:
        return "NQ.c.0"

    # ── Replay control (called from WebSocket handlers) ───────────

    def play(self) -> None:
        """Start continuous playback (disables step mode)."""
        self._step_mode = False
        self._pause_event.set()

    def pause(self) -> None:
        self._pause_event.clear()

    def step(self) -> None:
        """Advance one tick-bar worth of ticks, then re-pause."""
        self._step_mode = True
        self._pause_event.set()

    def set_speed(self, speed: float) -> None:
        self._speed = max(0.01, speed)

    def set_step_mode(self, enabled: bool) -> None:
        self._step_mode = enabled
        if not enabled:
            # Exiting step mode — resume playing
            self._pause_event.set()

    # ── Internal ──────────────────────────────────────────────────

    def _discover_dates(self) -> list[str]:
        """Get sorted dates with mbp10.parquet files."""
        dates = []
        for d in sorted(self._data_dir.iterdir()):
            if d.is_dir() and (d / "mbp10.parquet").exists():
                dates.append(d.name)
        return dates

    def _detect_front_month(
        self, conn: duckdb.DuckDBPyConnection, mbp_path: str,
    ) -> str:
        """Detect front-month symbol (highest trade count, no spreads)."""
        rows = conn.execute(f"""
            SELECT symbol, count(*) AS n
            FROM read_parquet('{mbp_path}')
            WHERE action = 'T' AND symbol NOT LIKE '%-%'
            GROUP BY symbol ORDER BY n DESC LIMIT 1
        """).fetchall()
        if not rows:
            raise ValueError(f"No trades in {mbp_path}")
        return rows[0][0]

    def _replay_loop(self) -> None:
        """Background thread: replay all dates through callbacks."""
        conn = duckdb.connect()

        try:
            # Phase 1: Pre-load prior dates at max speed
            self._preloading = True
            for date_str in self._preload_dates:
                if self._stop_flag:
                    return
                self._replay_one_date(conn, date_str)
            self._preloading = False

            # Phase 2: Visible replay with speed/pause control
            for date_str in self._visible_dates:
                if self._stop_flag:
                    return
                self._replay_one_date(conn, date_str)

            self.replay_complete = True
            logger.info("Replay complete")

            # Fire disconnected status
            for cb in self._status_callbacks:
                cb(ConnectionStatus.DISCONNECTED)

        except Exception:
            logger.exception("Replay loop error")
        finally:
            conn.close()

    def _replay_one_date(
        self, conn: duckdb.DuckDBPyConnection, date_str: str,
    ) -> None:
        """Replay one day's ticks through callbacks."""
        self.current_date = date_str

        # Fire file-loaded callbacks (used for preload level init)
        for cb in self._file_loaded_callbacks:
            cb(date_str)

        # Load data
        mbp_path = str(self._data_dir / date_str / "mbp10.parquet")
        try:
            front_month = self._detect_front_month(conn, mbp_path)
        except ValueError:
            logger.warning("No trades for %s — skipping", date_str)
            return

        df = conn.execute(f"""
            SELECT
                ts_event,
                CAST(price AS DOUBLE) AS price,
                CAST(size AS INTEGER) AS size,
                side,
                CAST(bid_px_00 AS DOUBLE) AS bid_price,
                CAST(ask_px_00 AS DOUBLE) AS ask_price,
                CAST(bid_sz_00 AS INTEGER) AS bid_size,
                CAST(ask_sz_00 AS INTEGER) AS ask_size
            FROM read_parquet('{mbp_path}')
            WHERE action = 'T'
              AND symbol = '{front_month}'
              AND bid_px_00 > 0 AND ask_px_00 > 0
            ORDER BY ts_event
        """).fetchdf()

        if df.empty:
            return

        prev_ts: pd.Timestamp | None = None

        for _, row in df.iterrows():
            if self._stop_flag:
                return

            # Pause check (skip during preload)
            if not self._preloading:
                self._pause_event.wait()

            # Parse timestamp
            ts = pd.Timestamp(row["ts_event"])
            if ts.tzinfo is None:
                ts = ts.tz_localize(UTC)
            ts_utc = ts.tz_convert(UTC).to_pydatetime()
            self.current_timestamp = ts_utc

            # Inter-tick delay (skip during preload or high speed)
            if not self._preloading and prev_ts is not None and self._speed < 50:
                delta = (ts - prev_ts).total_seconds()
                if delta > 0:
                    time_mod.sleep(delta / self._speed)
            prev_ts = ts

            # Build BBO update
            bid_price = Decimal(str(round(float(row["bid_price"]), 2)))
            ask_price = Decimal(str(round(float(row["ask_price"]), 2)))
            bbo = BBOUpdate(
                timestamp=ts_utc,
                bid_price=bid_price,
                bid_size=int(row["bid_size"]),
                ask_price=ask_price,
                ask_size=int(row["ask_size"]),
                symbol=front_month,
            )
            for cb in self._bbo_callbacks:
                cb(bbo)

            # Build trade update
            trade_price = Decimal(str(round(float(row["price"]), 2)))
            side = "BUY" if row["side"] == "A" else "SELL"
            trade = TradeUpdate(
                timestamp=ts_utc,
                price=trade_price,
                size=int(row["size"]),
                aggressor_side=side,
                symbol=front_month,
            )
            for cb in self._trade_callbacks:
                cb(trade)

            # Step mode: when a bar completes, the TickBarBuilder callback
            # sets _bar_complete_event synchronously (same thread). We
            # check it here and re-pause so the user can inspect the bar.
            if (
                not self._preloading
                and self._step_mode
                and self._bar_complete_event.is_set()
            ):
                self._bar_complete_event.clear()
                self._pause_event.clear()  # re-pause

    # No fetch_historical_ohlcv → PipelineService auto-skips backfill
