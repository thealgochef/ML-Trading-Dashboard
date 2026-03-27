"""
Phase 4 — Apex Account Simulator Tests

Tests the full Apex 4.0 50K account lifecycle simulation. Every rule
that governs real Apex accounts must be accurately replicated — the
entire purpose of paper trading is to validate the strategy under
production constraints before risking real money.

Business context: The trader runs multiple Apex 50K PA accounts with
$2,000 trailing drawdown. A blown account means ~$200 in eval + activation
costs lost. Accurate simulation prevents deploying a strategy that looks
profitable in theory but fails under Apex's specific constraints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.trading import (
    AccountStatus,
    ClosedTrade,
    Position,
)

from .conftest import force_balance, make_account

# ── Tests ────────────────────────────────────────────────────────


def test_initial_state():
    """New account starts at $50K, tier 1, liquidation at $48K."""
    acct = make_account()

    assert acct.balance == Decimal("50000")
    assert acct.status == AccountStatus.ACTIVE
    assert acct.tier == 1
    assert acct.max_contracts == 2
    assert acct.liquidation_threshold == Decimal("48000")
    assert acct.peak_balance == Decimal("50000")
    assert acct.safety_net_reached is False
    assert acct.payout_number == 0
    assert acct.qualifying_days == 0
    assert acct.has_position is False
    assert acct.profit == Decimal("0")


def test_open_close_position():
    """Open and close a winning trade, balance updates correctly."""
    acct = make_account()

    # Open LONG at 20100, 1 contract
    pos = acct.open_position(
        TradeDirection.LONG, Decimal("20100"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )
    assert isinstance(pos, Position)
    assert acct.has_position is True

    # Close at 20115 → +15 points → +$300
    trade = acct.close_position(
        Decimal("20115"), "tp",
        datetime(2026, 3, 2, 14, 35, tzinfo=UTC),
    )
    assert isinstance(trade, ClosedTrade)
    assert trade.pnl == Decimal("300")
    assert trade.pnl_points == Decimal("15")
    assert acct.balance == Decimal("50300")
    assert acct.has_position is False


def test_profit_updates_tier():
    """$1,500 profit moves account from tier 1 to tier 2."""
    acct = make_account()

    # Open and close for $1,500 profit (75 points)
    acct.open_position(
        TradeDirection.LONG, Decimal("20000"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )
    acct.close_position(
        Decimal("20075"), "tp",
        datetime(2026, 3, 2, 14, 35, tzinfo=UTC),
    )

    assert acct.balance == Decimal("51500")
    assert acct.profit == Decimal("1500")
    assert acct.tier == 2
    assert acct.max_contracts == 3


def test_tier_max_contracts():
    """Tier 1=2, tier 2=3, tier 3=4, tier 4=4 max contracts."""
    acct = make_account()

    # Tier 1: $0-$1,499
    assert acct.tier == 1
    assert acct.max_contracts == 2

    # Tier 2: $1,500-$2,999
    force_balance(acct, 51500)
    assert acct.tier == 2
    assert acct.max_contracts == 3

    # Tier 3: $3,000-$5,999
    force_balance(acct, 53000)
    assert acct.tier == 3
    assert acct.max_contracts == 4

    # Tier 4: $6,000+
    force_balance(acct, 56000)
    assert acct.tier == 4
    assert acct.max_contracts == 4


def test_dll_tier_1():
    """DLL is $1,000 at tiers 1-2."""
    acct = make_account()
    assert acct.daily_loss_limit == Decimal("1000")

    # Tier 2 also $1,000
    force_balance(acct, 51500)
    assert acct.tier == 2
    assert acct.daily_loss_limit == Decimal("1000")


def test_dll_tier_3():
    """DLL is $2,000 at tier 3."""
    acct = make_account()
    force_balance(acct, 53000)
    assert acct.tier == 3
    assert acct.daily_loss_limit == Decimal("2000")


def test_dll_tier_4():
    """DLL is $3,000 at tier 4."""
    acct = make_account()
    force_balance(acct, 56000)
    assert acct.tier == 4
    assert acct.daily_loss_limit == Decimal("3000")


def test_dll_breach_locks_account():
    """Exceeding DLL locks account for the day."""
    acct = make_account()
    # DLL at tier 1 = $1,000

    # Lose $1,000 (50 points)
    acct.open_position(
        TradeDirection.LONG, Decimal("20100"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )
    acct.close_position(
        Decimal("20050"), "sl",
        datetime(2026, 3, 2, 14, 35, tzinfo=UTC),
    )

    assert acct.daily_pnl == Decimal("-1000")
    assert acct.status == AccountStatus.DLL_LOCKED


def test_dll_resets_daily():
    """start_new_day() clears DLL lock and daily P&L."""
    acct = make_account()

    # Lose enough to trigger DLL lock
    acct.open_position(
        TradeDirection.LONG, Decimal("20100"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )
    acct.close_position(
        Decimal("20050"), "sl",
        datetime(2026, 3, 2, 14, 35, tzinfo=UTC),
    )
    assert acct.status == AccountStatus.DLL_LOCKED

    acct.start_new_day()

    assert acct.status == AccountStatus.ACTIVE
    assert acct.daily_pnl == Decimal("0")


def test_trailing_dd_trails_up():
    """Winning position moves liquidation threshold up."""
    acct = make_account()
    assert acct.liquidation_threshold == Decimal("48000")

    # Open LONG, price moves up
    acct.open_position(
        TradeDirection.LONG, Decimal("20000"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )

    # Price at 20050 → unrealized = $1,000 → equity = $51,000
    acct.update_unrealized(Decimal("20050"))

    # Peak should be $51,000, liquidation = $49,000
    assert acct.peak_balance == Decimal("51000")
    assert acct.liquidation_threshold == Decimal("49000")


def test_trailing_dd_does_not_trail_down():
    """Losing position doesn't move liquidation threshold down."""
    acct = make_account()

    acct.open_position(
        TradeDirection.LONG, Decimal("20000"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )

    # First move up
    acct.update_unrealized(Decimal("20050"))
    assert acct.liquidation_threshold == Decimal("49000")

    # Then move down — liquidation should NOT decrease
    acct.update_unrealized(Decimal("20010"))
    assert acct.liquidation_threshold == Decimal("49000")
    assert acct.peak_balance == Decimal("51000")


def test_safety_net_locks_threshold():
    """Peak balance reaching $52,100 locks liquidation at $50,100."""
    acct = make_account()

    acct.open_position(
        TradeDirection.LONG, Decimal("20000"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )

    # Push equity to $52,100 → peak triggers safety net
    # Need unrealized = $2,100 → 105 points above entry
    acct.update_unrealized(Decimal("20105"))

    assert acct.peak_balance == Decimal("52100")
    assert acct.safety_net_reached is True
    assert acct.liquidation_threshold == Decimal("50100")

    # Even higher peak — liquidation stays locked
    acct.update_unrealized(Decimal("20200"))
    assert acct.peak_balance == Decimal("54000")
    assert acct.liquidation_threshold == Decimal("50100")

    # Drop back down — liquidation still locked at $50,100
    acct.update_unrealized(Decimal("20010"))
    assert acct.liquidation_threshold == Decimal("50100")


def test_blown_on_threshold_breach():
    """Balance hitting liquidation threshold blows account."""
    acct = make_account()
    # Liquidation at $48,000

    acct.open_position(
        TradeDirection.LONG, Decimal("20100"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )

    # Drop 100 points → unrealized = -$2,000 → equity = $48,000 = liquidation
    acct.update_unrealized(Decimal("20000"))

    assert acct.status == AccountStatus.BLOWN


def test_qualifying_day_200_profit():
    """Day with $250+ profit counts as qualifying."""
    acct = make_account()

    # Make $260 profit (13 points × $20)
    acct.open_position(
        TradeDirection.LONG, Decimal("20100"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )
    acct.close_position(
        Decimal("20113"), "tp",
        datetime(2026, 3, 2, 14, 35, tzinfo=UTC),
    )

    acct.end_day()
    assert acct.qualifying_days == 1


def test_qualifying_day_under_200():
    """Day with < $250 profit doesn't count."""
    acct = make_account()

    # Make $100 profit (5 points)
    acct.open_position(
        TradeDirection.LONG, Decimal("20100"), 1,
        datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )
    acct.close_position(
        Decimal("20105"), "tp",
        datetime(2026, 3, 2, 14, 35, tzinfo=UTC),
    )

    acct.end_day()
    assert acct.qualifying_days == 0


def test_payout_eligibility():
    """5 qualifying days + sufficient balance + consistency = eligible."""
    acct = make_account()
    # Force state for eligibility
    acct._safety_net_reached = True
    acct._liquidation_threshold = Decimal("50100")
    acct._qualifying_days = 5
    force_balance(acct, 51000)

    # Need daily profits for consistency check (5 days of $200 each = $1,000 total)
    acct._daily_profits = [
        Decimal("200"), Decimal("200"), Decimal("200"),
        Decimal("200"), Decimal("200"),
    ]

    assert acct.payout_eligible is True


def test_consistency_rule():
    """Best day ≤ 50% of total profit required for payout."""
    acct = make_account()
    acct._safety_net_reached = True
    acct._liquidation_threshold = Decimal("50100")
    acct._qualifying_days = 5
    force_balance(acct, 51000)

    # Best day = $600, total = $1,000 → 60% > 50% → not consistent
    acct._daily_profits = [
        Decimal("600"), Decimal("100"), Decimal("100"),
        Decimal("100"), Decimal("100"),
    ]

    assert acct.consistency_rule_met is False
    assert acct.payout_eligible is False


def test_payout_caps():
    """Payout caps: 1=$1,500, 2=$2,000, 3=$2,500, 4=$2,500, 5=$3,000, 6=$3,000."""
    acct = make_account()

    assert acct.max_payout_amount == Decimal("1500")  # Payout 1

    acct._payout_number = 1
    assert acct.max_payout_amount == Decimal("2000")  # Payout 2

    acct._payout_number = 2
    assert acct.max_payout_amount == Decimal("2500")  # Payout 3

    acct._payout_number = 3
    assert acct.max_payout_amount == Decimal("2500")  # Payout 4

    acct._payout_number = 4
    assert acct.max_payout_amount == Decimal("3000")  # Payout 5

    acct._payout_number = 5
    assert acct.max_payout_amount == Decimal("3000")  # Payout 6


def test_retirement_after_6_payouts():
    """Account retires after 6th payout."""
    acct = make_account()
    acct._payout_number = 5  # Next is payout 6 (0-indexed: 6th payout)
    acct._safety_net_reached = True
    acct._liquidation_threshold = Decimal("50100")
    acct._qualifying_days = 5
    force_balance(acct, 54000)
    acct._daily_profits = [
        Decimal("200"), Decimal("200"), Decimal("200"),
        Decimal("200"), Decimal("200"),
    ]

    result = acct.request_payout(Decimal("3000"))

    assert result is True
    assert acct.status == AccountStatus.RETIRED
    assert acct.payout_number == 6


def test_minimum_balance_for_payout():
    """Payout requires balance - amount > liquidation_threshold.

    For a $500 minimum payout with locked liquidation at $50,100:
    - $50,600 balance: post-payout = $50,100 → NOT above liquidation → rejected
    - $50,601 balance: post-payout = $50,101 → above liquidation → accepted
    """
    acct = make_account()
    acct._safety_net_reached = True
    acct._liquidation_threshold = Decimal("50100")
    acct._qualifying_days = 5
    acct._daily_profits = [
        Decimal("200"), Decimal("200"), Decimal("200"),
        Decimal("200"), Decimal("200"),
    ]

    # $50,600 — post-payout $50,100 == liquidation, not strictly above → rejected
    force_balance(acct, 50600)
    assert acct.request_payout(Decimal("500")) is False

    # $50,601 — post-payout $50,101 > $50,100 → accepted
    force_balance(acct, 50601)
    assert acct.request_payout(Decimal("500")) is True
    assert acct.balance == Decimal("50101")

    # Also verify: $1,500 payout (cap #1) requires balance > $51,600
    acct2 = make_account(account_id="APEX-002")
    acct2._safety_net_reached = True
    acct2._liquidation_threshold = Decimal("50100")
    acct2._qualifying_days = 5
    acct2._daily_profits = [
        Decimal("400"), Decimal("400"), Decimal("400"),
        Decimal("400"), Decimal("400"),
    ]

    force_balance(acct2, 51600)
    assert acct2.request_payout(Decimal("1500")) is False

    force_balance(acct2, 51601)
    assert acct2.request_payout(Decimal("1500")) is True
