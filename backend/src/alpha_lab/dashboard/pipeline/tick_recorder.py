"""
Independent Parquet tick recorder.

Receives trade and BBO callbacks from the Rithmic client and persists them
to daily Parquet files. Completely decoupled from the rest of the system —
if the recorder crashes, the pipeline keeps working, and vice versa.

File layout: {output_dir}/{YYYY-MM-DD}.parquet
Date boundary: 6:00 PM ET (CME Globex trading day boundary)
Compression: snappy (matching Databento files)
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# CME Globex day boundary: 6:00 PM ET
_CME_DAY_BOUNDARY_HOUR = 18

_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("ns", tz="UTC")),
    ("record_type", pa.string()),
    ("price", pa.float64()),
    ("bid_price", pa.float64()),
    ("ask_price", pa.float64()),
    ("bid_size", pa.int32()),
    ("ask_size", pa.int32()),
    ("trade_size", pa.int32()),
    ("aggressor_side", pa.string()),
    ("symbol", pa.string()),
])


def _cme_trading_date(ts_utc: datetime) -> date:
    """Determine the CME trading date for a given UTC timestamp.

    CME Globex day boundary is 6:00 PM ET. Anything before 6 PM ET belongs
    to the current calendar date; 6 PM ET and after belongs to the next day.
    """
    ts_et = ts_utc.astimezone(ET)
    if ts_et.hour >= _CME_DAY_BOUNDARY_HOUR:
        return (ts_et + pd.Timedelta(days=1)).date()
    return ts_et.date()


class TickRecorder:
    """Records all incoming tick data to daily Parquet files."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        # Buffers keyed by trading date string
        self._buffers: dict[str, list[dict]] = {}
        self._last_flush = time.monotonic()

    def record_trade(self, trade: TradeUpdate) -> None:
        """Append a trade to the buffer."""
        row = {
            "timestamp": trade.timestamp,
            "record_type": "trade",
            "price": float(trade.price),
            "bid_price": None,
            "ask_price": None,
            "bid_size": None,
            "ask_size": None,
            "trade_size": trade.size,
            "aggressor_side": trade.aggressor_side,
            "symbol": trade.symbol,
        }
        self._append(trade.timestamp, row)

    def record_bbo(self, bbo: BBOUpdate) -> None:
        """Append a BBO update to the buffer."""
        mid = (float(bbo.bid_price) + float(bbo.ask_price)) / 2
        row = {
            "timestamp": bbo.timestamp,
            "record_type": "bbo",
            "price": mid,
            "bid_price": float(bbo.bid_price),
            "ask_price": float(bbo.ask_price),
            "bid_size": bbo.bid_size,
            "ask_size": bbo.ask_size,
            "trade_size": None,
            "aggressor_side": None,
            "symbol": bbo.symbol,
        }
        self._append(bbo.timestamp, row)

    def flush(self) -> None:
        """Force write all buffered data to disk."""
        with self._lock:
            for date_str, rows in self._buffers.items():
                if rows:
                    self._write_to_parquet(date_str, rows)
            self._buffers.clear()
            self._last_flush = time.monotonic()

    def close(self) -> None:
        """Flush and close the recorder."""
        self.flush()

    # ── Internal ──────────────────────────────────────────────────

    def _append(self, ts_utc: datetime, row: dict) -> None:
        trading_date = _cme_trading_date(ts_utc)
        date_str = trading_date.isoformat()

        with self._lock:
            if date_str not in self._buffers:
                self._buffers[date_str] = []
            self._buffers[date_str].append(row)

    def _write_to_parquet(self, date_str: str, rows: list[dict]) -> None:
        """Write rows to the daily Parquet file, appending if it exists."""
        filepath = self._output_dir / f"{date_str}.parquet"

        # Build new table from rows
        new_table = pa.Table.from_pylist(rows, schema=_SCHEMA)

        if filepath.exists():
            existing = pq.read_table(filepath, schema=_SCHEMA)
            combined = pa.concat_tables([existing, new_table])
        else:
            combined = new_table

        pq.write_table(
            combined,
            filepath,
            compression="snappy",
            row_group_size=50000,
        )
