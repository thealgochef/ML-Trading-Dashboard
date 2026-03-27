"""
Phase 2 — Feature Parity Tests

Validates that the live feature computer produces results consistent with
the batch experiment code in src/alpha_lab/experiment/features.py for the
same input data. This is the most important test in the entire system —
if features diverge, the model is useless.

These tests load historical events from the experiment, extract raw tick data
from MBP-10 Parquet files, convert to TradeUpdate/BBOUpdate format, run
through the live FeatureComputer, and compare with batch-computed values.

Tolerances:
- Absorption ratio: 0.01 absolute (trade-based, should match closely)
- Tempo features: 1.0s absolute (MBP-10 has deeper book events that
  affect event density; extracted top-of-book is sparser)
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from alpha_lab.dashboard.engine.feature_computer import FeatureComputer
from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate

DATA_DIR = Path("data/databento")
LABELED_EVENTS_PATH = Path("data/experiment/labeled_events.parquet")
FEATURE_MATRIX_PATH = Path("data/experiment/feature_matrix.parquet")

# Skip all parity tests if data files are missing
pytestmark = pytest.mark.skipif(
    not LABELED_EVENTS_PATH.exists() or not FEATURE_MATRIX_PATH.exists(),
    reason="Experiment data files not found",
)


def _load_parity_events() -> list[dict]:
    """Load 5 resolved events with their batch-computed feature values.

    Returns list of dicts with event metadata + expected feature values.
    """
    le = pd.read_parquet(LABELED_EVENTS_PATH)
    fm = pd.read_parquet(FEATURE_MATRIX_PATH)

    resolved = le[le["label"] != "no_resolution"].reset_index(drop=True)

    events = []
    for i in range(min(5, len(resolved))):
        ev = resolved.iloc[i]
        event_ts = pd.Timestamp(ev["event_ts"])

        # Match feature matrix row by event_ts
        # labeled_events has tz-aware ET timestamps; feature_matrix has tz-naive UTC
        event_ts_utc_naive = event_ts.tz_convert("UTC").tz_localize(None)
        fm_match = fm[fm["event_ts"] == event_ts_utc_naive]
        if fm_match.empty:
            continue

        fm_row = fm_match.iloc[0]

        events.append({
            "event_ts": event_ts,
            "date": ev["date"],
            "direction": ev["direction"],
            "representative_price": ev["representative_price"],
            "expected_beyond": fm_row["int_time_beyond_level"],
            "expected_within": fm_row["int_time_within_2pts"],
            "expected_absorption": fm_row["int_absorption_ratio"],
        })

    return events


def _get_front_month_symbol(date_str: str) -> str:
    """Return front-month NQ contract symbol for a given date."""
    from datetime import date

    dt = date.fromisoformat(date_str)
    if dt < date(2025, 12, 15):
        return "NQZ5"
    return "NQH6"


def _load_window_data(
    event_ts: pd.Timestamp,
    date_str: str,
) -> tuple[list[TradeUpdate], list[BBOUpdate]]:
    """Extract TradeUpdate and BBOUpdate from MBP-10 for a 5-min window.

    Converts MBP-10 format to the live system's typed dataclasses.
    """
    symbol = _get_front_month_symbol(date_str)

    # Load MBP-10 data for the event date
    event_ts_utc = event_ts.tz_convert("UTC")
    event_date_utc = event_ts_utc.date()

    mbp_path = DATA_DIR / "NQ" / str(event_date_utc) / "mbp10.parquet"
    if not mbp_path.exists():
        # Try the trading date directly
        mbp_path = DATA_DIR / "NQ" / date_str / "mbp10.parquet"
    if not mbp_path.exists():
        pytest.skip(f"MBP-10 data not found at {mbp_path}")

    mbp = pd.read_parquet(mbp_path)

    # Filter to window [event_ts, event_ts + 5min]
    window_end = event_ts_utc + pd.Timedelta(minutes=5)
    mask = (
        (mbp["ts_event"] >= event_ts_utc)
        & (mbp["ts_event"] <= window_end)
        & (mbp["symbol"] == symbol)
        & (mbp["bid_px_00"] > 0)
        & (mbp["ask_px_00"] > 0)
    )
    window = mbp[mask].sort_values("ts_event")

    trades: list[TradeUpdate] = []
    bbo_updates: list[BBOUpdate] = []

    # Track last BBO to avoid duplicate snapshots
    last_bid = None
    last_ask = None

    for _, row in window.iterrows():
        ts = row["ts_event"].to_pydatetime()

        if row["action"] == "T":
            trades.append(TradeUpdate(
                timestamp=ts,
                price=Decimal(str(row["price"])),
                size=int(row["size"]),
                aggressor_side="BUY" if row["side"] == "A" else "SELL",
                symbol=symbol,
            ))

        # Emit BBO update whenever top-of-book changes
        bid = row["bid_px_00"]
        ask = row["ask_px_00"]
        if bid != last_bid or ask != last_ask:
            bbo_updates.append(BBOUpdate(
                timestamp=ts,
                bid_price=Decimal(str(bid)),
                bid_size=int(row["bid_sz_00"]),
                ask_price=Decimal(str(ask)),
                ask_size=int(row["ask_sz_00"]),
                symbol=symbol,
            ))
            last_bid = bid
            last_ask = ask

    return trades, bbo_updates


# Cache loaded events across tests
_PARITY_EVENTS: list[dict] | None = None


def _get_events() -> list[dict]:
    global _PARITY_EVENTS
    if _PARITY_EVENTS is None:
        _PARITY_EVENTS = _load_parity_events()
    return _PARITY_EVENTS


def _run_parity(event_index: int) -> None:
    """Run parity test for a specific event index."""
    events = _get_events()
    if event_index >= len(events):
        pytest.skip(f"Event {event_index} not available")

    ev = events[event_index]
    fc = FeatureComputer()

    event_ts = ev["event_ts"]
    event_ts_utc = event_ts.tz_convert("UTC")
    window_start = event_ts_utc.to_pydatetime()
    window_end = window_start + timedelta(minutes=5)
    level_price = Decimal(str(ev["representative_price"]))
    direction = TradeDirection.LONG if ev["direction"] == "LONG" else TradeDirection.SHORT

    trades, bbo_updates = _load_window_data(event_ts, ev["date"])

    if not trades and not bbo_updates:
        pytest.skip("No tick data loaded for event")

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=level_price,
        direction=direction,
        window_start=window_start,
        window_end=window_end,
    )

    # Absorption: tight tolerance (trade-based, same data)
    assert abs(result["int_absorption_ratio"] - ev["expected_absorption"]) < 0.01, (
        f"Absorption mismatch: live={result['int_absorption_ratio']:.4f} "
        f"batch={ev['expected_absorption']:.4f}"
    )

    # Tempo: wider tolerance (event density differs between MBP-10 and extracted BBO)
    assert abs(result["int_time_beyond_level"] - ev["expected_beyond"]) < 1.0, (
        f"Time beyond mismatch: live={result['int_time_beyond_level']:.4f} "
        f"batch={ev['expected_beyond']:.4f}"
    )
    assert abs(result["int_time_within_2pts"] - ev["expected_within"]) < 1.0, (
        f"Time within mismatch: live={result['int_time_within_2pts']:.4f} "
        f"batch={ev['expected_within']:.4f}"
    )


def test_parity_event_1():
    """Replay historical event 1, compare features within tolerance."""
    _run_parity(0)


def test_parity_event_2():
    """Replay historical event 2."""
    _run_parity(1)


def test_parity_event_3():
    """Replay historical event 3."""
    _run_parity(2)


def test_parity_event_4():
    """Replay historical event 4."""
    _run_parity(3)


def test_parity_event_5():
    """Replay historical event 5."""
    _run_parity(4)
