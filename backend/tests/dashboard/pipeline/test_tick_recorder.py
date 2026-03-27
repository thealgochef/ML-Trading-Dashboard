"""
Phase 1 — Tick Recorder Tests

Tests the independent Parquet file recording system. The tick recorder runs
as a completely decoupled process that persists every tick from the Rithmic
stream to daily Parquet files. It has zero dependency on the rest of the
dashboard system.

Business context: Every tick received from Rithmic is stored indefinitely,
building a proprietary historical database that replaces Databento for future
backtesting. The files must be compatible with the existing Databento Parquet
files in data/databento/NQ/ for unified analysis.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow.parquet as pq
import pytest

from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate
from alpha_lab.dashboard.pipeline.tick_recorder import TickRecorder

ET = ZoneInfo("America/New_York")

# ── Helpers ──────────────────────────────────────────────────────


def _trade(
    ts: datetime | None = None,
    price: float = 20100.25,
    size: int = 3,
    side: str = "BUY",
    symbol: str = "NQH6",
) -> TradeUpdate:
    if ts is None:
        ts = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side=side,
        symbol=symbol,
    )


def _bbo(
    ts: datetime | None = None,
    bid: float = 20100.00,
    ask: float = 20100.25,
    bid_size: int = 15,
    ask_size: int = 12,
    symbol: str = "NQH6",
) -> BBOUpdate:
    if ts is None:
        ts = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    return BBOUpdate(
        timestamp=ts,
        bid_price=Decimal(str(bid)),
        bid_size=bid_size,
        ask_price=Decimal(str(ask)),
        ask_size=ask_size,
        symbol=symbol,
    )


# ── Tests ────────────────────────────────────────────────────────


def test_creates_daily_file(tmp_path: Path):
    """First trade creates a new Parquet file named YYYY-MM-DD.parquet."""
    recorder = TickRecorder(tmp_path)
    recorder.record_trade(_trade())
    recorder.flush()
    recorder.close()

    # 2026-03-02 14:30 UTC = 2026-03-02 09:30 ET = CME day 2026-03-02
    files = list(tmp_path.glob("*.parquet"))
    assert len(files) == 1
    assert files[0].name == "2026-03-02.parquet"


def test_records_trade_fields(tmp_path: Path):
    """Trade records contain all schema fields with correct values."""
    recorder = TickRecorder(tmp_path)
    recorder.record_trade(
        _trade(price=20100.25, size=3, side="BUY", symbol="NQH6")
    )
    recorder.flush()
    recorder.close()

    df = pd.read_parquet(tmp_path / "2026-03-02.parquet")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["record_type"] == "trade"
    assert row["price"] == pytest.approx(20100.25)
    assert row["trade_size"] == 3
    assert row["aggressor_side"] == "BUY"
    assert row["symbol"] == "NQH6"
    assert pd.isna(row["bid_price"])
    assert pd.isna(row["ask_price"])


def test_records_bbo_fields(tmp_path: Path):
    """BBO records contain all schema fields, trade-specific fields are null."""
    recorder = TickRecorder(tmp_path)
    recorder.record_bbo(
        _bbo(bid=20100.00, ask=20100.25, bid_size=15, ask_size=12)
    )
    recorder.flush()
    recorder.close()

    df = pd.read_parquet(tmp_path / "2026-03-02.parquet")
    row = df.iloc[0]
    assert row["record_type"] == "bbo"
    assert row["bid_price"] == pytest.approx(20100.00)
    assert row["ask_price"] == pytest.approx(20100.25)
    assert row["bid_size"] == 15
    assert row["ask_size"] == 12
    assert pd.isna(row["trade_size"]) or row["trade_size"] == 0
    assert pd.isna(row["aggressor_side"]) or row["aggressor_side"] == ""


def test_appends_to_existing_file(tmp_path: Path):
    """If today's file exists, new data is appended, not overwritten."""
    ts = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # First session
    r1 = TickRecorder(tmp_path)
    r1.record_trade(_trade(ts=ts))
    r1.flush()
    r1.close()

    # Second session — appends
    ts2 = ts + timedelta(seconds=10)
    r2 = TickRecorder(tmp_path)
    r2.record_trade(_trade(ts=ts2))
    r2.flush()
    r2.close()

    df = pd.read_parquet(tmp_path / "2026-03-02.parquet")
    assert len(df) == 2


def test_date_boundary_rollover(tmp_path: Path):
    """At 6:00 PM ET (CME day boundary), a new file is created."""
    # 5:59 PM ET = still current day
    ts_before = datetime(2026, 3, 2, 17, 59, 0, tzinfo=ET).astimezone(UTC)
    # 6:00 PM ET = next CME day
    ts_after = datetime(2026, 3, 2, 18, 0, 0, tzinfo=ET).astimezone(UTC)

    recorder = TickRecorder(tmp_path)
    recorder.record_trade(_trade(ts=ts_before))
    recorder.record_trade(_trade(ts=ts_after))
    recorder.flush()
    recorder.close()

    files = sorted(f.name for f in tmp_path.glob("*.parquet"))
    assert len(files) == 2
    assert "2026-03-02.parquet" in files
    assert "2026-03-03.parquet" in files


def test_flush_writes_to_disk(tmp_path: Path):
    """After flush(), data is readable from the Parquet file."""
    recorder = TickRecorder(tmp_path)
    recorder.record_trade(_trade())
    # Before flush — file may not exist yet
    recorder.flush()

    # After flush — file must be readable
    df = pd.read_parquet(tmp_path / "2026-03-02.parquet")
    assert len(df) == 1

    recorder.close()


def test_close_flushes_remaining(tmp_path: Path):
    """close() writes all remaining buffered data."""
    recorder = TickRecorder(tmp_path)
    recorder.record_trade(_trade())
    # No explicit flush — close should handle it
    recorder.close()

    df = pd.read_parquet(tmp_path / "2026-03-02.parquet")
    assert len(df) == 1


def test_output_schema(tmp_path: Path):
    """Parquet file schema matches the specification exactly."""
    recorder = TickRecorder(tmp_path)
    recorder.record_trade(_trade())
    recorder.flush()
    recorder.close()

    schema = pq.read_schema(tmp_path / "2026-03-02.parquet")
    field_names = set(schema.names)
    expected = {
        "timestamp", "record_type", "price",
        "bid_price", "ask_price", "bid_size", "ask_size",
        "trade_size", "aggressor_side", "symbol",
    }
    assert expected.issubset(field_names), f"Missing: {expected - field_names}"


def test_snappy_compression(tmp_path: Path):
    """Files are compressed with snappy."""
    recorder = TickRecorder(tmp_path)
    recorder.record_trade(_trade())
    recorder.flush()
    recorder.close()

    meta = pq.read_metadata(tmp_path / "2026-03-02.parquet")
    compression = meta.row_group(0).column(0).compression
    assert compression.lower() == "snappy"


def test_empty_recorder_close(tmp_path: Path):
    """Closing a recorder with no data does not create an empty file."""
    recorder = TickRecorder(tmp_path)
    recorder.close()

    files = list(tmp_path.glob("*.parquet"))
    assert len(files) == 0


def test_concurrent_writes(tmp_path: Path):
    """Multiple threads can record simultaneously without corruption."""
    import threading

    recorder = TickRecorder(tmp_path)

    def write_trades(offset: int):
        for i in range(50):
            ts = datetime(2026, 3, 2, 14, 30, offset, i * 1000, tzinfo=UTC)
            recorder.record_trade(_trade(ts=ts, price=20100.0 + i))

    threads = [threading.Thread(target=write_trades, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    recorder.flush()
    recorder.close()

    df = pd.read_parquet(tmp_path / "2026-03-02.parquet")
    assert len(df) == 200  # 4 threads x 50 trades
