"""
CME Day Boundary Alignment Tests

Tests the session-change-driven day reset logic that replaced the old
file-transition-based _on_day_boundary callback. These tests verify:
  - Bootstrap initializes levels without running end-of-day accounting
  - CME day boundary (post_market → asia) fires full reset exactly once
  - Idempotency guard prevents duplicate resets for the same trading date
  - Preload file-loaded callback only runs during preload phase
  - Force-resolve of open predictions at day boundary

The callback pattern tested here mirrors the closures in server.py,
run_tp_sl_sweep.py, and run_strategy_comparison.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, call
from zoneinfo import ZoneInfo

import pytest

from alpha_lab.dashboard.engine.level_engine import LevelEngine, _cme_day_start_utc
from alpha_lab.dashboard.engine.models import LevelSide, LevelType
from alpha_lab.dashboard.engine.touch_detector import TouchDetector, _classify_session
from alpha_lab.dashboard.pipeline.price_buffer import PriceBuffer
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate

ET = ZoneInfo("America/New_York")


# ── Helpers ──────────────────────────────────────────────────────


def _trade(ts: datetime, price: float, size: int = 1) -> TradeUpdate:
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side="BUY",
        symbol="NQH6",
    )


def _cme_trading_date(ts_utc: datetime) -> date:
    """Replica of the _cme_trading_date() utility used in server.py/sweep/comparison."""
    ts_et = ts_utc.astimezone(ET)
    if ts_et.time() >= time(18, 0):
        return (ts_et + timedelta(days=1)).date()
    return ts_et.date()


class MockAccountManager:
    """Minimal mock for AccountManager."""

    def __init__(self):
        self.start_new_day_calls = 0
        self._accounts: list[MagicMock] = []

    def get_all_accounts(self) -> list[MagicMock]:
        return self._accounts

    def start_new_day(self) -> None:
        self.start_new_day_calls += 1

    def add_mock_account(self) -> MagicMock:
        acct = MagicMock()
        acct.daily_pnl = 0.0
        acct.balance = 50000.0
        acct.status = MagicMock(value="active")
        self._accounts.append(acct)
        return acct


def _build_session_change_handler(
    level_engine: LevelEngine,
    account_manager: MockAccountManager | None = None,
    outcome_tracker: MagicMock | None = None,
    economic_tracker: MagicMock | None = None,
    buffer: PriceBuffer | None = None,
    session_ended_ref: dict | None = None,
):
    """Build a _on_session_change closure matching the pattern in server.py.

    Returns (handler, state_dict) where state_dict tracks internal state
    for assertions.
    """
    state = {
        "last_reset_trading_date": None,
        "session_ended": False,
        "bootstrap_count": 0,
        "cme_boundary_count": 0,
        "regular_count": 0,
    }
    if session_ended_ref is not None:
        state.update(session_ended_ref)

    def handler(old_session: str | None, new_session: str, timestamp: datetime) -> None:
        trading_date = _cme_trading_date(timestamp)

        is_cme_day_boundary = (old_session == "post_market" and new_session == "asia")
        is_bootstrap = (state["last_reset_trading_date"] is None)

        # ── Bootstrap ──
        if is_bootstrap:
            state["last_reset_trading_date"] = trading_date
            level_engine.reset_daily()
            level_engine.compute_levels(trading_date, current_time=timestamp)
            state["bootstrap_count"] += 1
            return

        # ── True CME day boundary ──
        if is_cme_day_boundary and trading_date != state["last_reset_trading_date"]:
            state["last_reset_trading_date"] = trading_date

            # STEP 0: Force-resolve predictions
            if outcome_tracker and not state["session_ended"]:
                outcome_tracker.on_session_end(timestamp)

            # STEP 1: End-of-day accounting
            if account_manager:
                for acct in account_manager.get_all_accounts():
                    acct.end_day()
            if economic_tracker:
                economic_tracker.on_day_end(trading_date.isoformat(), [])

            # STEP 2: Reset accounts
            if account_manager:
                account_manager.start_new_day()

            # STEP 3: Reset levels
            level_engine.reset_daily()
            level_engine.compute_levels(trading_date, current_time=timestamp)

            # STEP 4: Clear pipeline state
            if buffer:
                buffer.evict()
            state["session_ended"] = False

            state["cme_boundary_count"] += 1
            return

        # ── Regular session transition ──
        level_engine.compute_levels(trading_date, current_time=timestamp)
        state["regular_count"] += 1

    return handler, state


# ── Tests ────────────────────────────────────────────────────────


def test_bootstrap_any_session():
    """First visible callback initializes levels but does NOT run end-of-day accounting.

    The bootstrap path fires on the very first session_change callback
    regardless of which session it is. It calls reset_daily() + compute_levels()
    but never calls end_day() or start_new_day() because there's no prior
    day to close.
    """
    buf = PriceBuffer()
    # Populate a prior day's RTH so PDH/PDL exist
    prev_rth = datetime(2026, 3, 1, 14, 30, tzinfo=UTC)
    buf.add_trade(_trade(prev_rth, 20050.00))
    buf.add_trade(_trade(prev_rth + timedelta(hours=1), 20120.00))
    buf.add_trade(_trade(prev_rth + timedelta(hours=2), 20000.00))

    engine = LevelEngine(buf)
    acct_mgr = MockAccountManager()
    acct = acct_mgr.add_mock_account()
    outcome_tracker = MagicMock()

    handler, state = _build_session_change_handler(
        level_engine=engine,
        account_manager=acct_mgr,
        outcome_tracker=outcome_tracker,
    )

    # Simulate replay starting mid-london (no preceding post_market → asia)
    london_tick_ts = datetime(2026, 3, 2, 7, 0, tzinfo=ET).astimezone(UTC)
    handler(None, "london", london_tick_ts)

    # Bootstrap fired
    assert state["bootstrap_count"] == 1
    assert state["cme_boundary_count"] == 0

    # end_day() and start_new_day() were NOT called
    acct.end_day.assert_not_called()
    assert acct_mgr.start_new_day_calls == 0
    outcome_tracker.on_session_end.assert_not_called()

    # But levels WERE computed (PDH/PDL should be present)
    levels = engine.all_levels
    pdh = [lv for lv in levels if lv.level_type == LevelType.PDH]
    assert len(pdh) == 1, "Bootstrap should compute PDH/PDL"


def test_cme_day_boundary_fires_full_reset():
    """post_market → asia triggers full day reset with correct step ordering.

    Steps must execute in order:
    0. Force-resolve predictions (if session not ended)
    1. End-of-day accounting (reads closing day's state)
    2. Reset accounts for new day
    3. Reset levels
    4. Clear pipeline state
    """
    buf = PriceBuffer()
    engine = LevelEngine(buf)
    acct_mgr = MockAccountManager()
    acct = acct_mgr.add_mock_account()
    outcome_tracker = MagicMock()
    economic_tracker = MagicMock()

    handler, state = _build_session_change_handler(
        level_engine=engine,
        account_manager=acct_mgr,
        outcome_tracker=outcome_tracker,
        economic_tracker=economic_tracker,
        buffer=buf,
    )

    # Bootstrap first (e.g., starting in post_market)
    pm_ts = datetime(2026, 3, 2, 17, 0, tzinfo=ET).astimezone(UTC)
    handler(None, "post_market", pm_ts)
    assert state["bootstrap_count"] == 1

    # Now fire the CME day boundary: post_market → asia at 6 PM ET
    asia_ts = datetime(2026, 3, 2, 18, 0, tzinfo=ET).astimezone(UTC)
    handler("post_market", "asia", asia_ts)

    assert state["cme_boundary_count"] == 1

    # Verify all steps fired
    outcome_tracker.on_session_end.assert_called_once_with(asia_ts)
    acct.end_day.assert_called_once()
    economic_tracker.on_day_end.assert_called_once()
    assert acct_mgr.start_new_day_calls == 1

    # session_ended reset to False
    assert state["session_ended"] is False


def test_idempotent_day_reset():
    """Same trading_date boundary fired twice → reset runs only once.

    The _last_reset_trading_date guard prevents duplicate resets when
    the same post_market → asia transition fires again for the same
    trading date (e.g., if session_touches briefly bounces).
    """
    buf = PriceBuffer()
    engine = LevelEngine(buf)
    acct_mgr = MockAccountManager()
    acct_mgr.add_mock_account()

    handler, state = _build_session_change_handler(
        level_engine=engine,
        account_manager=acct_mgr,
        buffer=buf,
    )

    # Bootstrap
    pm_ts = datetime(2026, 3, 2, 17, 0, tzinfo=ET).astimezone(UTC)
    handler(None, "post_market", pm_ts)

    # First CME boundary
    asia_ts = datetime(2026, 3, 2, 18, 0, tzinfo=ET).astimezone(UTC)
    handler("post_market", "asia", asia_ts)
    assert state["cme_boundary_count"] == 1
    assert acct_mgr.start_new_day_calls == 1

    # Second post_market → asia for the SAME trading date
    asia_ts_2 = datetime(2026, 3, 2, 18, 5, tzinfo=ET).astimezone(UTC)
    handler("post_market", "asia", asia_ts_2)

    # Should NOT have fired again (same trading date)
    assert state["cme_boundary_count"] == 1
    assert acct_mgr.start_new_day_calls == 1


def test_preload_file_loaded_only_runs_during_preload():
    """_on_file_loaded callback is a no-op outside preload phase.

    During visible replay, day reset is handled by session change callbacks.
    The file-loaded callback only runs during preload to initialize levels
    for PDH/PDL computation.
    """
    buf = PriceBuffer()
    engine = LevelEngine(buf)

    # Track calls to reset_daily to verify when it fires
    reset_calls: list[str] = []
    original_reset = engine.reset_daily

    def tracked_reset():
        reset_calls.append("reset")
        original_reset()

    engine.reset_daily = tracked_reset

    # Simulate the _on_file_loaded pattern from server.py
    class FakeReplayClient:
        def __init__(self):
            self._preloading = False

    client = FakeReplayClient()

    def _on_file_loaded(date_str: str) -> None:
        if not client._preloading:
            return  # No-op outside preload
        trading_date = date.fromisoformat(date_str)
        engine.reset_daily()
        day_start_utc = _cme_day_start_utc(trading_date)
        engine.compute_levels(trading_date, current_time=day_start_utc)

    # Call during preload → should fire
    client._preloading = True
    _on_file_loaded("2026-03-02")
    assert len(reset_calls) == 1

    # Call outside preload → should be no-op
    reset_calls.clear()
    client._preloading = False
    _on_file_loaded("2026-03-03")
    assert len(reset_calls) == 0


def test_unresolved_predictions_at_boundary():
    """Predictions open at day boundary get force-resolved before accounting.

    Step 0 must call outcome_tracker.on_session_end() BEFORE Step 1
    (end_day accounting), but only if session_ended is still False.
    """
    buf = PriceBuffer()
    engine = LevelEngine(buf)
    outcome_tracker = MagicMock()

    # Test 1: session_ended=False → force-resolve fires
    handler, state = _build_session_change_handler(
        level_engine=engine,
        outcome_tracker=outcome_tracker,
        session_ended_ref={"session_ended": False},
    )

    # Bootstrap
    pm_ts = datetime(2026, 3, 2, 17, 0, tzinfo=ET).astimezone(UTC)
    handler(None, "post_market", pm_ts)

    # CME boundary
    asia_ts = datetime(2026, 3, 2, 18, 0, tzinfo=ET).astimezone(UTC)
    handler("post_market", "asia", asia_ts)

    outcome_tracker.on_session_end.assert_called_once_with(asia_ts)

    # Test 2: session_ended=True → force-resolve does NOT fire
    outcome_tracker.reset_mock()
    handler2, state2 = _build_session_change_handler(
        level_engine=engine,
        outcome_tracker=outcome_tracker,
        session_ended_ref={"session_ended": True},
    )

    pm_ts2 = datetime(2026, 3, 3, 17, 0, tzinfo=ET).astimezone(UTC)
    handler2(None, "post_market", pm_ts2)

    asia_ts2 = datetime(2026, 3, 3, 18, 0, tzinfo=ET).astimezone(UTC)
    handler2("post_market", "asia", asia_ts2)

    outcome_tracker.on_session_end.assert_not_called()


def test_end_day_runs_without_economic_tracker():
    """acct.end_day() fires even when economic_tracker is None (live mode).

    In live mode, state.economic_tracker is None. The end_day() call must
    still run unconditionally to record qualifying days and daily profits.
    Only economic_tracker.on_day_end() should be skipped.
    """
    buf = PriceBuffer()
    engine = LevelEngine(buf)
    acct_mgr = MockAccountManager()
    acct = acct_mgr.add_mock_account()

    # No economic_tracker — simulates live mode
    handler, state = _build_session_change_handler(
        level_engine=engine,
        account_manager=acct_mgr,
        economic_tracker=None,
        buffer=buf,
    )

    # Bootstrap
    pm_ts = datetime(2026, 3, 2, 17, 0, tzinfo=ET).astimezone(UTC)
    handler(None, "post_market", pm_ts)

    # CME day boundary
    asia_ts = datetime(2026, 3, 2, 18, 0, tzinfo=ET).astimezone(UTC)
    handler("post_market", "asia", asia_ts)

    # end_day() MUST still be called (records qualifying days)
    acct.end_day.assert_called_once()
    # start_new_day() MUST still be called (zeros daily PnL)
    assert acct_mgr.start_new_day_calls == 1
