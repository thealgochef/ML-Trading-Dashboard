"""
Shared test fixtures for Phase 4 — Paper Trading Engine.

Provides helper functions for creating test accounts, trade updates,
and positions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate
from alpha_lab.dashboard.trading import AccountStatus
from alpha_lab.dashboard.trading.apex_account import ApexAccount

BASE_TS = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)


def make_account(
    account_id: str = "APEX-001",
    label: str = "Test Account",
    eval_cost: float = 147.0,
    activation_cost: float = 85.0,
    group: str = "A",
) -> ApexAccount:
    """Create an ApexAccount for testing."""
    return ApexAccount(
        account_id=account_id,
        label=label,
        eval_cost=Decimal(str(eval_cost)),
        activation_cost=Decimal(str(activation_cost)),
        group=group,
    )


def make_trade_update(
    ts_offset_s: float = 0,
    price: float = 20100.00,
    size: int = 5,
) -> TradeUpdate:
    """Create a TradeUpdate at BASE_TS + offset."""
    ts = BASE_TS + timedelta(seconds=ts_offset_s)
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side="BUY",
        symbol="NQH6",
    )


def force_balance(account: ApexAccount, balance: float) -> None:
    """Set account balance directly for testing edge cases."""
    account._balance = Decimal(str(balance))


def force_status(account: ApexAccount, status: AccountStatus) -> None:
    """Set account status directly for testing."""
    account._status = status
