"""
Phase 4 — Account Manager Tests

Tests portfolio-level account management: grouping, aggregate stats,
state persistence, and daily lifecycle operations.

Business context: The trader runs 5 accounts (3 Group A, 2 Group B)
simultaneously. The account manager tracks totals, filters eligible
accounts for trading, and handles daily resets across all accounts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.trading import AccountStatus
from alpha_lab.dashboard.trading.account_manager import AccountManager

from .conftest import force_balance, force_status

# ── Tests ────────────────────────────────────────────────────────


def test_add_account_to_group():
    """Account added to correct group."""
    mgr = AccountManager()

    acct_a = mgr.add_account("Acct A1", Decimal("147"), Decimal("85"), "A")
    acct_b = mgr.add_account("Acct B1", Decimal("147"), Decimal("85"), "B")

    group_a = mgr.get_accounts_by_group("A")
    group_b = mgr.get_accounts_by_group("B")

    assert len(group_a) == 1
    assert group_a[0].account_id == acct_a.account_id
    assert group_a[0].group == "A"

    assert len(group_b) == 1
    assert group_b[0].account_id == acct_b.account_id
    assert group_b[0].group == "B"


def test_get_tradeable_accounts():
    """Returns only active, non-DLL-locked, no-position accounts."""
    mgr = AccountManager()

    acct1 = mgr.add_account("Active", Decimal("147"), Decimal("85"), "A")
    acct2 = mgr.add_account("DLL Locked", Decimal("147"), Decimal("85"), "A")
    acct3 = mgr.add_account("Has Position", Decimal("147"), Decimal("85"), "A")

    # Lock acct2
    force_status(acct2, AccountStatus.DLL_LOCKED)

    # Give acct3 a position
    acct3.open_position(
        TradeDirection.LONG, Decimal("20100"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )

    tradeable = mgr.get_tradeable_accounts()

    assert len(tradeable) == 1
    assert tradeable[0].account_id == acct1.account_id


def test_portfolio_summary():
    """Aggregate stats calculated correctly."""
    mgr = AccountManager()

    acct1 = mgr.add_account("A1", Decimal("147"), Decimal("85"), "A")
    acct2 = mgr.add_account("B1", Decimal("147"), Decimal("85"), "B")

    # acct1 wins $500
    force_balance(acct1, 50500)
    # acct2 loses $300
    force_balance(acct2, 49700)

    summary = mgr.get_portfolio_summary()

    assert summary["total_invested"] == Decimal("464")  # 2 * (147 + 85)
    assert summary["total_balance"] == Decimal("100200")
    assert summary["total_profit"] == Decimal("200")  # +500 - 300
    assert summary["active_count"] == 2


def test_start_new_day_resets_all():
    """Resets DLL and daily P&L for all active accounts."""
    mgr = AccountManager()

    acct1 = mgr.add_account("A1", Decimal("147"), Decimal("85"), "A")
    acct2 = mgr.add_account("A2", Decimal("147"), Decimal("85"), "A")

    # Lock one via DLL
    force_status(acct1, AccountStatus.DLL_LOCKED)

    mgr.start_new_day()

    assert acct1.status == AccountStatus.ACTIVE
    assert acct1.daily_pnl == Decimal("0")
    assert acct2.daily_pnl == Decimal("0")


def test_save_and_load_state():
    """Account state round-trips through serialization."""
    mgr = AccountManager()

    acct = mgr.add_account("Test", Decimal("147"), Decimal("85"), "A")
    force_balance(acct, 51500)
    acct._safety_net_reached = True

    state = mgr.save_state()

    # Create new manager and load
    mgr2 = AccountManager()
    mgr2.load_state(state)

    loaded = mgr2.get_all_accounts()
    assert len(loaded) == 1
    assert loaded[0].balance == Decimal("51500")
    assert loaded[0].safety_net_reached is True
    assert loaded[0].group == "A"


def test_blown_account_excluded_from_trading():
    """Blown accounts not returned by get_tradeable."""
    mgr = AccountManager()

    acct1 = mgr.add_account("Active", Decimal("147"), Decimal("85"), "A")
    acct2 = mgr.add_account("Blown", Decimal("147"), Decimal("85"), "A")

    force_status(acct2, AccountStatus.BLOWN)

    tradeable = mgr.get_tradeable_accounts()

    assert len(tradeable) == 1
    assert tradeable[0].account_id == acct1.account_id


def test_retired_account_excluded():
    """Retired accounts not returned by get_tradeable."""
    mgr = AccountManager()

    acct1 = mgr.add_account("Active", Decimal("147"), Decimal("85"), "A")
    acct2 = mgr.add_account("Retired", Decimal("147"), Decimal("85"), "A")

    force_status(acct2, AccountStatus.RETIRED)

    tradeable = mgr.get_tradeable_accounts()

    assert len(tradeable) == 1
    assert tradeable[0].account_id == acct1.account_id


def test_all_accounts_includes_historical():
    """get_all_accounts includes blown and retired."""
    mgr = AccountManager()

    mgr.add_account("Active", Decimal("147"), Decimal("85"), "A")
    acct2 = mgr.add_account("Blown", Decimal("147"), Decimal("85"), "A")
    acct3 = mgr.add_account("Retired", Decimal("147"), Decimal("85"), "A")

    force_status(acct2, AccountStatus.BLOWN)
    force_status(acct3, AccountStatus.RETIRED)

    all_accts = mgr.get_all_accounts()
    assert len(all_accts) == 3

    active = mgr.get_active_accounts()
    assert len(active) == 1
