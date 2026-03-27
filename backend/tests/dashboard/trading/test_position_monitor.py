"""
Phase 4 — Position Monitor Tests

Tests real-time position monitoring against the live tick stream.
The position monitor enforces TP/SL/DLL/flatten rules on every tick.

Business context: Position monitoring runs on every single trade tick.
A missed TP means leaving money on the table. A missed SL means
exceeding risk limits. A missed flatten means violating Apex rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.trading import AccountStatus
from alpha_lab.dashboard.trading.account_manager import AccountManager
from alpha_lab.dashboard.trading.position_monitor import PositionMonitor
from alpha_lab.dashboard.trading.trade_executor import TradeExecutor

from .conftest import make_trade_update


def _setup_monitor(
    n_group_a: int = 1,
    n_group_b: int = 1,
) -> tuple[PositionMonitor, TradeExecutor, AccountManager]:
    """Create monitor with executor and populated manager."""
    mgr = AccountManager()
    for i in range(n_group_a):
        mgr.add_account(f"A{i + 1}", Decimal("147"), Decimal("85"), "A")
    for i in range(n_group_b):
        mgr.add_account(f"B{i + 1}", Decimal("147"), Decimal("85"), "B")
    executor = TradeExecutor(mgr)
    monitor = PositionMonitor(mgr, executor)
    return monitor, executor, mgr


def _open_positions(
    executor: TradeExecutor,
    direction: TradeDirection = TradeDirection.LONG,
    price: float = 20100.00,
) -> None:
    """Open positions on all eligible accounts."""
    pred = {
        "is_executable": True,
        "trade_direction": direction,
        "level_price": Decimal(str(price)),
        "event_id": "test",
    }
    executor.on_prediction(pred, datetime(2026, 3, 2, 14, 30, tzinfo=UTC))


# ── Tests ────────────────────────────────────────────────────────


def test_tp_hit_group_a():
    """Position closed at 15-point profit for Group A."""
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=0)
    _open_positions(executor)

    # Trade at +15 points → TP hit for Group A
    trade = make_trade_update(ts_offset_s=60, price=20115.00)
    closed = monitor.on_trade(trade)

    assert len(closed) == 1
    assert closed[0].exit_reason == "tp"
    assert closed[0].pnl == Decimal("300")  # 15 pts * $20


def test_tp_hit_group_b():
    """Position closed at 30-point profit for Group B."""
    monitor, executor, mgr = _setup_monitor(n_group_a=0, n_group_b=1)
    _open_positions(executor)

    # +15 points — not enough for Group B
    trade1 = make_trade_update(ts_offset_s=30, price=20115.00)
    closed1 = monitor.on_trade(trade1)
    assert len(closed1) == 0

    # +30 points — TP hit for Group B
    trade2 = make_trade_update(ts_offset_s=60, price=20130.00)
    closed2 = monitor.on_trade(trade2)

    assert len(closed2) == 1
    assert closed2[0].exit_reason == "tp"
    assert closed2[0].pnl == Decimal("600")  # 30 pts * $20


def test_sl_hit():
    """Position closed at configured stop loss."""
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=0)
    # Default SL for Group A = 15 pts (1:1 R:R)
    _open_positions(executor)

    # Drop 15 points → SL hit
    trade = make_trade_update(ts_offset_s=60, price=20085.00)
    closed = monitor.on_trade(trade)

    assert len(closed) == 1
    assert closed[0].exit_reason == "sl"
    assert closed[0].pnl == Decimal("-300")


def test_unrealized_pnl_updates():
    """Unrealized P&L tracks with each tick."""
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=0)
    _open_positions(executor)

    acct = mgr.get_all_accounts()[0]

    # Tick at +5 points
    trade = make_trade_update(ts_offset_s=30, price=20105.00)
    monitor.on_trade(trade)

    assert acct.current_position.unrealized_pnl == Decimal("100")  # 5 * $20


def test_trailing_dd_checked_on_tick():
    """Trailing drawdown evaluated on every tick."""
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=0)
    _open_positions(executor)

    acct = mgr.get_all_accounts()[0]
    initial_liq = acct.liquidation_threshold

    # Tick at +10 points → equity $50,200 → liquidation should trail
    trade = make_trade_update(ts_offset_s=30, price=20110.00)
    monitor.on_trade(trade)

    assert acct.liquidation_threshold > initial_liq
    assert acct.peak_balance == Decimal("50200")


def test_account_blown_mid_trade():
    """Account blown during trade = force close at liquidation."""
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=0)
    _open_positions(executor)

    acct = mgr.get_all_accounts()[0]

    # Drop 100 points → equity = $48,000 = liquidation → blown
    trade = make_trade_update(ts_offset_s=60, price=20000.00)
    closed = monitor.on_trade(trade)

    assert len(closed) == 1
    assert closed[0].exit_reason == "blown"
    assert acct.status == AccountStatus.BLOWN


def test_dll_breach_mid_trade():
    """DLL breach = close position, lock account."""
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=0)
    # Tier 1 DLL = $1,000

    _open_positions(executor)

    acct = mgr.get_all_accounts()[0]

    # Drop 50 points → unrealized = -$1,000 = DLL breach
    trade = make_trade_update(ts_offset_s=60, price=20050.00)
    closed = monitor.on_trade(trade)

    assert len(closed) == 1
    assert closed[0].exit_reason == "dll"
    assert acct.status == AccountStatus.DLL_LOCKED


def test_multiple_accounts_independent():
    """Group A can hit TP while Group B stays open."""
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=1)
    _open_positions(executor)

    accts = mgr.get_all_accounts()
    acct_a = [a for a in accts if a.group == "A"][0]
    acct_b = [a for a in accts if a.group == "B"][0]

    # +15 points: Group A TP hit, Group B still open (needs 30)
    trade = make_trade_update(ts_offset_s=60, price=20115.00)
    closed = monitor.on_trade(trade)

    assert len(closed) == 1
    assert closed[0].group == "A"
    assert not acct_a.has_position
    assert acct_b.has_position


def test_configurable_tp_sl():
    """Changing TP/SL settings takes effect immediately."""
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=0)

    # Change Group A TP from 15 to 10
    monitor.set_group_tp("A", Decimal("10"))
    _open_positions(executor)

    # +10 should now trigger TP (was 15)
    trade = make_trade_update(ts_offset_s=60, price=20110.00)
    closed = monitor.on_trade(trade)

    assert len(closed) == 1
    assert closed[0].exit_reason == "tp"
    assert closed[0].pnl == Decimal("200")  # 10 * $20


def test_hard_flatten_timer():
    """Flatten fires at exactly 15:55 ET.

    March 2, 2026 is EST (UTC-5). 15:55 EST = 20:55 UTC.
    """
    monitor, executor, mgr = _setup_monitor(n_group_a=1, n_group_b=1)
    _open_positions(executor)

    # Before flatten time — no action (15:54:59 EST = 20:54:59 UTC)
    pre_time = datetime(2026, 3, 2, 20, 54, 59, tzinfo=UTC)
    closed_pre = monitor.check_flatten_time(pre_time, Decimal("20105"))

    assert len(closed_pre) == 0

    # At flatten time — everything closes (15:55:00 EST = 20:55:00 UTC)
    flatten_time = datetime(2026, 3, 2, 20, 55, 0, tzinfo=UTC)
    closed = monitor.check_flatten_time(flatten_time, Decimal("20105"))

    assert len(closed) == 2
    assert all(t.exit_reason == "flatten" for t in closed)
