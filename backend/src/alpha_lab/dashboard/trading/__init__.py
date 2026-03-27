"""
Phase 4 data models for the paper trading engine.

Defines enums, dataclasses, and constants for Apex 4.0 account simulation,
position management, and trade execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from alpha_lab.dashboard.engine.models import TradeDirection


class AccountStatus(Enum):
    ACTIVE = "active"
    DLL_LOCKED = "dll_locked"
    BLOWN = "blown"
    RETIRED = "retired"


@dataclass
class Position:
    """An open position on a simulated account."""

    account_id: str
    direction: TradeDirection
    entry_price: Decimal
    contracts: int
    entry_time: datetime
    unrealized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    mfe: Decimal = field(default_factory=lambda: Decimal("0"))
    mae: Decimal = field(default_factory=lambda: Decimal("0"))


@dataclass
class ClosedTrade:
    """A completed trade with P&L."""

    account_id: str
    direction: TradeDirection
    entry_price: Decimal
    exit_price: Decimal
    contracts: int
    entry_time: datetime
    exit_time: datetime
    pnl: Decimal  # In dollars
    pnl_points: Decimal  # In NQ points
    exit_reason: str  # 'tp', 'sl', 'flatten', 'manual', 'dll', 'blown'
    group: str


# ── Apex 4.0 50K Constants ─────────────────────────────────────

NQ_POINT_VALUE = Decimal("20")  # $20 per point per contract

STARTING_BALANCE = Decimal("50000")
TRAILING_DD = Decimal("2000")
STARTING_LIQUIDATION = Decimal("48000")
SAFETY_NET_PEAK = Decimal("52100")
SAFETY_NET_LIQUIDATION = Decimal("50100")

# Tier thresholds: (min_profit, max_profit, max_contracts, dll)
TIER_THRESHOLDS = [
    (Decimal("0"), Decimal("1500"), 2, Decimal("1000")),       # Tier 1
    (Decimal("1500"), Decimal("3000"), 3, Decimal("1000")),    # Tier 2
    (Decimal("3000"), Decimal("6000"), 4, Decimal("2000")),    # Tier 3
    (Decimal("6000"), None, 4, Decimal("3000")),               # Tier 4
]

# Payout caps by payout number (1-indexed)
PAYOUT_CAPS = [
    Decimal("1500"),  # Payout 1
    Decimal("2000"),  # Payout 2
    Decimal("2500"),  # Payout 3
    Decimal("2500"),  # Payout 4
    Decimal("3000"),  # Payout 5
    Decimal("3000"),  # Payout 6
]

MAX_PAYOUTS = 6
QUALIFYING_DAY_MIN_PROFIT = Decimal("250")
QUALIFYING_DAYS_REQUIRED = 5
CONSISTENCY_RULE_PCT = Decimal("0.50")  # Best day ≤ 50% of total profit
MIN_PAYOUT_AMOUNT = Decimal("500")
