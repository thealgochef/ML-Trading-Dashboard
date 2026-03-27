"""
Phase 4 — Trade Executor Tests

Tests trade execution across multiple accounts, signal handling modes,
and the no-hedging constraint.

Business context: When the model predicts "tradeable_reversal" during
NY RTH, all eligible accounts enter simultaneously. The executor must
handle edge cases like some accounts being DLL-locked, conflicting
signal modes, and the absolute no-hedging rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.trading import AccountStatus, ClosedTrade, Position
from alpha_lab.dashboard.trading.account_manager import AccountManager
from alpha_lab.dashboard.trading.trade_executor import TradeExecutor

from .conftest import force_status


def _make_executable_prediction(
    direction: TradeDirection = TradeDirection.LONG,
    level_price: float = 20100.00,
    event_id: str = "test_event_1",
):
    """Create a minimal executable prediction dict for testing."""
    return {
        "is_executable": True,
        "trade_direction": direction,
        "level_price": Decimal(str(level_price)),
        "event_id": event_id,
    }


def _setup_executor(
    n_group_a: int = 2,
    n_group_b: int = 1,
) -> tuple[TradeExecutor, AccountManager]:
    """Create a TradeExecutor with populated AccountManager."""
    mgr = AccountManager()
    for i in range(n_group_a):
        mgr.add_account(f"A{i + 1}", Decimal("147"), Decimal("85"), "A")
    for i in range(n_group_b):
        mgr.add_account(f"B{i + 1}", Decimal("147"), Decimal("85"), "B")
    executor = TradeExecutor(mgr)
    return executor, mgr


# ── Tests ────────────────────────────────────────────────────────


def test_executable_prediction_opens_positions():
    """Reversal during RTH opens positions on all eligible accounts."""
    executor, mgr = _setup_executor(n_group_a=2, n_group_b=1)
    pred = _make_executable_prediction()

    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    positions = executor.on_prediction(pred, now)

    assert len(positions) == 3
    assert all(isinstance(p, Position) for p in positions)
    assert all(p.direction == TradeDirection.LONG for p in positions)

    # All accounts should now have positions
    for acct in mgr.get_all_accounts():
        assert acct.has_position is True


def test_non_executable_prediction_no_trades():
    """Non-executable prediction does nothing."""
    executor, mgr = _setup_executor()
    pred = {"is_executable": False}

    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    positions = executor.on_prediction(pred, now)

    assert len(positions) == 0
    assert all(not a.has_position for a in mgr.get_all_accounts())


def test_ignore_mode_skips_positioned_accounts():
    """In ignore mode, accounts with positions are skipped."""
    executor, mgr = _setup_executor(n_group_a=2, n_group_b=0)
    executor.second_signal_mode = "ignore"

    # First signal — opens on both
    pred1 = _make_executable_prediction(direction=TradeDirection.LONG)
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    executor.on_prediction(pred1, now)

    # Second signal same direction — both already have positions, skipped
    pred2 = _make_executable_prediction(
        direction=TradeDirection.LONG, event_id="test_event_2",
    )
    positions = executor.on_prediction(pred2, now)

    assert len(positions) == 0


def test_flip_mode_closes_then_opens():
    """In flip mode, existing positions closed before new opposite entry."""
    executor, mgr = _setup_executor(n_group_a=1, n_group_b=0)
    executor.second_signal_mode = "flip"

    # First signal LONG
    pred1 = _make_executable_prediction(direction=TradeDirection.LONG)
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    executor.on_prediction(pred1, now)

    acct = mgr.get_all_accounts()[0]
    assert acct.current_position.direction == TradeDirection.LONG

    # Second signal SHORT — should close LONG, open SHORT
    pred2 = _make_executable_prediction(
        direction=TradeDirection.SHORT,
        level_price=20100.00,
        event_id="test_event_2",
    )
    now2 = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    positions = executor.on_prediction(pred2, now2, current_price=Decimal("20105"))

    assert len(positions) == 1
    assert acct.current_position.direction == TradeDirection.SHORT


def test_no_hedging_enforced():
    """Cannot have long and short positions simultaneously across accounts."""
    executor, mgr = _setup_executor(n_group_a=2, n_group_b=0)
    executor.second_signal_mode = "ignore"

    # Open LONG on account 1
    pred1 = _make_executable_prediction(direction=TradeDirection.LONG)
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    executor.on_prediction(pred1, now)

    # Close position on acct1 only, acct2 still has LONG
    acct1 = mgr.get_all_accounts()[0]
    acct1.close_position(
        Decimal("20110"), "tp",
        datetime(2026, 3, 2, 14, 35, tzinfo=UTC),
    )

    # Try SHORT — should be blocked because acct2 is still LONG
    pred2 = _make_executable_prediction(
        direction=TradeDirection.SHORT, event_id="test_event_2",
    )
    positions = executor.on_prediction(pred2, now)

    assert len(positions) == 0


def test_close_all_positions():
    """Closes everything across all accounts."""
    executor, mgr = _setup_executor(n_group_a=2, n_group_b=1)

    pred = _make_executable_prediction()
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    executor.on_prediction(pred, now)

    close_time = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    trades = executor.close_all_positions(
        Decimal("20110"), "manual", close_time,
    )

    assert len(trades) == 3
    assert all(isinstance(t, ClosedTrade) for t in trades)
    assert all(not a.has_position for a in mgr.get_all_accounts())


def test_close_single_account():
    """Close one account without affecting others."""
    executor, mgr = _setup_executor(n_group_a=2, n_group_b=0)

    pred = _make_executable_prediction()
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    executor.on_prediction(pred, now)

    accts = mgr.get_all_accounts()
    close_time = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    trade = executor.close_account_position(
        accts[0].account_id, Decimal("20110"), "manual", close_time,
    )

    assert isinstance(trade, ClosedTrade)
    assert not accts[0].has_position
    assert accts[1].has_position  # Other account unaffected


def test_manual_entry():
    """Manual buy/sell on a specific account works."""
    executor, mgr = _setup_executor(n_group_a=1, n_group_b=0)

    acct = mgr.get_all_accounts()[0]
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

    pos = executor.manual_entry(
        acct.account_id, TradeDirection.LONG, Decimal("20100"), now,
    )

    assert isinstance(pos, Position)
    assert acct.has_position is True
    assert acct.current_position.direction == TradeDirection.LONG


def test_hard_flatten_closes_everything():
    """3:55 PM CT hard flatten closes all, no exceptions."""
    executor, mgr = _setup_executor(n_group_a=2, n_group_b=1)

    pred = _make_executable_prediction()
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    executor.on_prediction(pred, now)

    flatten_time = datetime(2026, 3, 2, 19, 55, tzinfo=UTC)  # 15:55 ET = 19:55 UTC
    trades = executor.hard_flatten(Decimal("20105"), flatten_time)

    assert len(trades) == 3
    assert all(t.exit_reason == "flatten" for t in trades)
    assert all(not a.has_position for a in mgr.get_all_accounts())


def test_dll_locked_excluded():
    """DLL-locked accounts don't receive new trades."""
    executor, mgr = _setup_executor(n_group_a=2, n_group_b=0)

    # Lock one account
    accts = mgr.get_all_accounts()
    force_status(accts[0], AccountStatus.DLL_LOCKED)

    pred = _make_executable_prediction()
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    positions = executor.on_prediction(pred, now)

    assert len(positions) == 1
    assert not accts[0].has_position
    assert accts[1].has_position


def test_callbacks_fire_on_open_close():
    """on_trade_opened and on_trade_closed callbacks fire."""
    executor, mgr = _setup_executor(n_group_a=1, n_group_b=0)

    opened: list[Position] = []
    closed: list[ClosedTrade] = []
    executor.on_trade_opened(lambda p: opened.append(p))
    executor.on_trade_closed(lambda t: closed.append(t))

    pred = _make_executable_prediction()
    now = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    executor.on_prediction(pred, now)

    assert len(opened) == 1

    close_time = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    executor.close_all_positions(Decimal("20110"), "manual", close_time)

    assert len(closed) == 1
