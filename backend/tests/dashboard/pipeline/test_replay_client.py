"""
Phase C — ReplayClient Tests

Tests the Parquet-replay client that duck-types the DatabentoClient
interface for replaying historical tick data through the dashboard.
"""

from __future__ import annotations

import asyncio
import struct
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from alpha_lab.dashboard.pipeline.replay_client import ReplayClient
from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    TradeUpdate,
)


def _create_test_data(tmp_path: Path, dates: list[str]) -> Path:
    """Create minimal Parquet files for testing.

    Each date gets an mbp10.parquet with a handful of trade rows.
    """
    data_dir = tmp_path / "NQ"
    for date_str in dates:
        date_dir = data_dir / date_str
        date_dir.mkdir(parents=True)

        n_trades = 20
        base_ts = pd.Timestamp(f"{date_str} 14:30:00", tz="UTC")
        records = []
        for i in range(n_trades):
            records.append({
                "ts_event": base_ts + pd.Timedelta(milliseconds=i * 100),
                "price": 20100.0 + i * 0.25,
                "size": 1 + (i % 3),
                "side": "A" if i % 2 == 0 else "B",
                "bid_px_00": 20099.75 + i * 0.25,
                "ask_px_00": 20100.25 + i * 0.25,
                "bid_sz_00": 10,
                "ask_sz_00": 12,
                "action": "T",
                "symbol": "NQM5",
            })

        df = pd.DataFrame(records)
        df.to_parquet(date_dir / "mbp10.parquet", index=False)

    return data_dir


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Create test data with 5 dates."""
    dates = [
        "2025-06-02", "2025-06-03", "2025-06-04",
        "2025-06-05", "2025-06-06",
    ]
    return _create_test_data(tmp_path, dates)


def test_implements_client_interface(data_dir: Path):
    """ReplayClient exposes all methods expected by PipelineService."""
    client = ReplayClient(data_dir=data_dir)
    assert callable(client.on_trade)
    assert callable(client.on_bbo)
    assert callable(client.on_connection_status)
    assert callable(getattr(client, "connect", None))
    assert callable(getattr(client, "disconnect", None))
    assert callable(getattr(client, "subscribe_market_data", None))
    assert callable(getattr(client, "get_front_month_contract", None))
    # Must NOT have fetch_historical_ohlcv
    assert not hasattr(client, "fetch_historical_ohlcv")


@pytest.mark.asyncio
async def test_discovers_dates(data_dir: Path):
    """connect() discovers all available dates."""
    client = ReplayClient(data_dir=data_dir)
    await client.connect()
    assert len(client._all_dates) == 5
    assert client._all_dates[0] == "2025-06-02"
    assert client._all_dates[-1] == "2025-06-06"


@pytest.mark.asyncio
async def test_play_pause(data_dir: Path):
    """play/pause toggles the internal threading.Event."""
    client = ReplayClient(data_dir=data_dir, speed=1000.0)
    await client.connect()

    # Initially clear (not playing until subscribe_market_data)
    assert not client._pause_event.is_set()

    client.play()
    assert client._pause_event.is_set()

    client.pause()
    assert not client._pause_event.is_set()


@pytest.mark.asyncio
async def test_speed_change(data_dir: Path):
    """set_speed updates the internal speed value."""
    client = ReplayClient(data_dir=data_dir)
    await client.connect()

    client.set_speed(50.0)
    assert client._speed == 50.0

    client.set_speed(0.001)
    assert client._speed == 0.01  # Clamped to minimum


@pytest.mark.asyncio
async def test_emits_trade_updates(data_dir: Path):
    """ReplayClient fires trade callbacks with correct TradeUpdate objects."""
    client = ReplayClient(data_dir=data_dir, speed=1000.0)
    # Disable step mode and start playing so replay runs to completion
    client._step_mode = False

    trades: list[TradeUpdate] = []
    client.on_trade(trades.append)

    await client.connect()
    client.play()
    await client.subscribe_market_data("NQ.c.0")

    # Wait for replay to finish (small dataset, fast speed)
    await asyncio.sleep(2.0)
    await client.disconnect()

    # 5 dates × 20 trades each = 100 total
    assert len(trades) == 100
    assert all(isinstance(t, TradeUpdate) for t in trades)
    assert all(t.price > 0 for t in trades)


@pytest.mark.asyncio
async def test_respects_date_range(data_dir: Path):
    """start_date/end_date filters visible dates correctly."""
    client = ReplayClient(
        data_dir=data_dir,
        start_date="2025-06-04",
        end_date="2025-06-05",
        speed=1000.0,
    )
    # Disable step mode and start playing so replay runs to completion
    client._step_mode = False

    trades: list[TradeUpdate] = []
    client.on_trade(trades.append)

    await client.connect()

    # Visible: 06-04, 06-05 (2 dates)
    # Preload: 06-02, 06-03 (2 prior dates)
    assert client._visible_dates == ["2025-06-04", "2025-06-05"]
    assert client._preload_dates == ["2025-06-02", "2025-06-03"]

    client.play()
    await client.subscribe_market_data("NQ.c.0")
    await asyncio.sleep(2.0)
    await client.disconnect()

    # preload (2×20) + visible (2×20) = 80
    assert len(trades) == 80


@pytest.mark.asyncio
async def test_preloads_prior_dates(data_dir: Path):
    """2 dates are pre-loaded before visible range for PDH/PDL."""
    client = ReplayClient(
        data_dir=data_dir,
        start_date="2025-06-04",
        speed=1000.0,
    )
    await client.connect()

    assert len(client._preload_dates) == 2
    assert client._preload_dates == ["2025-06-02", "2025-06-03"]


@pytest.mark.asyncio
async def test_day_boundary_callback_fires(data_dir: Path):
    """on_day_boundary callback fires before each day's ticks."""
    client = ReplayClient(data_dir=data_dir, speed=1000.0)
    # Disable step mode and start playing so replay runs to completion
    client._step_mode = False

    boundaries: list[str] = []
    client.on_day_boundary(boundaries.append)

    await client.connect()
    client.play()
    await client.subscribe_market_data("NQ.c.0")
    await asyncio.sleep(2.0)
    await client.disconnect()

    # 2 preload + 3 visible = 5 total day boundaries
    assert len(boundaries) == 5
    assert boundaries == [
        "2025-06-02", "2025-06-03", "2025-06-04",
        "2025-06-05", "2025-06-06",
    ]
