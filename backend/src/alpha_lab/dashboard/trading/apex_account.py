"""
Apex Account Simulator — full Apex 4.0 50K account lifecycle.

Simulates balance tracking, trailing drawdown, tier-based scaling, DLL,
payout eligibility, and the 50% consistency rule. One instance per
simulated account.

NQ point value: $20 per point per contract.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.trading import (
    CONSISTENCY_RULE_PCT,
    MAX_PAYOUTS,
    NQ_POINT_VALUE,
    PAYOUT_CAPS,
    QUALIFYING_DAY_MIN_PROFIT,
    QUALIFYING_DAYS_REQUIRED,
    SAFETY_NET_LIQUIDATION,
    SAFETY_NET_PEAK,
    STARTING_BALANCE,
    STARTING_LIQUIDATION,
    TIER_THRESHOLDS,
    TRAILING_DD,
    AccountStatus,
    ClosedTrade,
    Position,
)


class ApexAccount:
    """Simulates one Apex 4.0 50K Intraday Trailing Drawdown PA.

    Tracks balance, trailing drawdown, tier, DLL, payout eligibility,
    and consistency rules. Enforces all Apex 4.0 lifecycle constraints
    from activation through 6 payouts to retirement.
    """

    def __init__(
        self,
        account_id: str,
        label: str,
        eval_cost: Decimal,
        activation_cost: Decimal,
        group: str,
        dd_type: str = "intraday",
    ) -> None:
        self.account_id = account_id
        self.label = label
        self.eval_cost = eval_cost
        self.activation_cost = activation_cost
        self.group = group
        self._dd_type = dd_type  # "intraday" or "eod"
        self.wave: str | None = None  # "scout", "confirmer", "sniper", or None

        # Balance & state
        self._balance = STARTING_BALANCE
        self._status = AccountStatus.ACTIVE
        self._peak_balance = STARTING_BALANCE
        self._liquidation_threshold = STARTING_LIQUIDATION
        self._safety_net_reached = False

        # Payout tracking
        self._payout_number = 0  # Next payout number (0 = first payout not yet taken)
        self._qualifying_days = 0
        self._total_payouts = Decimal("0")
        self._daily_profits: list[Decimal] = []

        # Daily state
        self._daily_realized_pnl = Decimal("0")

        # Position
        self._position: Position | None = None

    # ── Balance & State Properties ──────────────────────────────

    @property
    def balance(self) -> Decimal:
        return self._balance

    @property
    def profit(self) -> Decimal:
        return self._balance - STARTING_BALANCE

    @property
    def status(self) -> AccountStatus:
        return self._status

    @property
    def tier(self) -> int:
        profit = self.profit
        for i, (min_p, max_p, _, _) in enumerate(TIER_THRESHOLDS):
            if (max_p is None or profit < max_p) and profit >= min_p:
                return i + 1
        return 1

    @property
    def max_contracts(self) -> int:
        return TIER_THRESHOLDS[self.tier - 1][2]

    @property
    def daily_loss_limit(self) -> Decimal:
        return TIER_THRESHOLDS[self.tier - 1][3]

    # ── Trailing Drawdown Properties ────────────────────────────

    @property
    def liquidation_threshold(self) -> Decimal:
        return self._liquidation_threshold

    @property
    def peak_balance(self) -> Decimal:
        return self._peak_balance

    @property
    def safety_net_reached(self) -> bool:
        return self._safety_net_reached

    @property
    def trailing_dd_remaining(self) -> Decimal:
        return self._balance - self._liquidation_threshold

    # ── Payout Properties ───────────────────────────────────────

    @property
    def payout_number(self) -> int:
        return self._payout_number

    @property
    def qualifying_days(self) -> int:
        return self._qualifying_days

    @property
    def max_payout_amount(self) -> Decimal:
        if self._payout_number >= MAX_PAYOUTS:
            return Decimal("0")
        return PAYOUT_CAPS[self._payout_number]

    @property
    def consistency_rule_met(self) -> bool:
        if not self._daily_profits:
            return False
        total = sum(self._daily_profits)
        if total <= 0:
            return False
        best_day = max(self._daily_profits)
        return best_day <= total * CONSISTENCY_RULE_PCT

    @property
    def payout_eligible(self) -> bool:
        if self._status not in (AccountStatus.ACTIVE, AccountStatus.DLL_LOCKED):
            return False
        if not self._safety_net_reached:
            return False
        if self._qualifying_days < QUALIFYING_DAYS_REQUIRED:
            return False
        if self._payout_number >= MAX_PAYOUTS:
            return False
        if not self.consistency_rule_met:
            return False
        # Must have enough balance for at least the minimum payout
        # balance - min_payout > liquidation_threshold
        min_payout = min(self.max_payout_amount, Decimal("500"))
        return self._balance - min_payout > self._liquidation_threshold

    # ── DLL Properties ──────────────────────────────────────────

    @property
    def daily_pnl(self) -> Decimal:
        unrealized = self._position.unrealized_pnl if self._position else Decimal("0")
        return self._daily_realized_pnl + unrealized

    @property
    def dll_remaining(self) -> Decimal:
        return self.daily_loss_limit + self.daily_pnl

    @property
    def dll_locked(self) -> bool:
        return self._status == AccountStatus.DLL_LOCKED

    # ── Position Properties ─────────────────────────────────────

    @property
    def has_position(self) -> bool:
        return self._position is not None

    @property
    def current_position(self) -> Position | None:
        return self._position

    # ── Trading Methods ─────────────────────────────────────────

    def open_position(
        self,
        direction: TradeDirection,
        entry_price: Decimal,
        contracts: int,
        entry_time: datetime,
    ) -> Position:
        """Open a new position on this account."""
        if self._position is not None:
            raise ValueError(f"Account {self.account_id} already has an open position")
        if self._status != AccountStatus.ACTIVE:
            raise ValueError(f"Account {self.account_id} is {self._status.value}")
        if contracts > self.max_contracts:
            raise ValueError(
                f"Contracts {contracts} exceeds tier {self.tier} max {self.max_contracts}",
            )

        self._position = Position(
            account_id=self.account_id,
            direction=direction,
            entry_price=entry_price,
            contracts=contracts,
            entry_time=entry_time,
        )
        return self._position

    def close_position(
        self,
        exit_price: Decimal,
        reason: str,
        exit_time: datetime,
    ) -> ClosedTrade:
        """Close the current position and update account state."""
        if self._position is None:
            raise ValueError(f"Account {self.account_id} has no open position")

        pos = self._position
        pnl_points = self._compute_pnl_points(pos, exit_price)
        pnl_dollars = pnl_points * NQ_POINT_VALUE * pos.contracts

        # Update balance
        self._balance += pnl_dollars
        self._daily_realized_pnl += pnl_dollars

        trade = ClosedTrade(
            account_id=self.account_id,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            contracts=pos.contracts,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            pnl=pnl_dollars,
            pnl_points=pnl_points,
            exit_reason=reason,
            group=self.group,
        )

        # Clear position
        self._position = None

        # Update peak balance and trailing DD after realized P&L
        self._update_trailing_dd(self._balance)

        # Check DLL after close
        self._check_dll()

        return trade

    def update_unrealized(self, current_price: Decimal) -> None:
        """Update unrealized P&L, MFE/MAE, and trailing drawdown.

        Called on every tick while a position is open.
        """
        if self._position is None:
            return
        if self._status == AccountStatus.BLOWN:
            return

        pos = self._position
        pnl_points = self._compute_pnl_points(pos, current_price)
        unrealized = pnl_points * NQ_POINT_VALUE * pos.contracts

        pos.unrealized_pnl = unrealized
        pos.mfe = max(pos.mfe, unrealized)
        pos.mae = min(pos.mae, unrealized)

        # Current equity = realized balance + unrealized
        current_equity = self._balance + unrealized

        # Update trailing drawdown
        self._update_trailing_dd(current_equity)

        # Check blown
        if current_equity <= self._liquidation_threshold:
            self._status = AccountStatus.BLOWN
            return

        # Check DLL (includes unrealized)
        self._check_dll()

    def start_new_day(self) -> None:
        """Reset daily counters. Called at session open."""
        self._daily_realized_pnl = Decimal("0")
        if self._status == AccountStatus.DLL_LOCKED:
            self._status = AccountStatus.ACTIVE

    def end_day(self) -> None:
        """Record daily profit for qualifying day tracking."""
        if self._daily_realized_pnl >= QUALIFYING_DAY_MIN_PROFIT:
            self._qualifying_days += 1
        if self._daily_realized_pnl > 0:
            self._daily_profits.append(self._daily_realized_pnl)

    def request_payout(self, amount: Decimal) -> bool:
        """Attempt a payout. Returns False if not eligible.

        Validates: balance - amount > liquidation_threshold
        """
        if not self.payout_eligible:
            return False
        if amount > self.max_payout_amount:
            return False
        if not (self._balance - amount > self._liquidation_threshold):
            return False

        self._balance -= amount
        self._total_payouts += amount
        self._payout_number += 1
        self._qualifying_days = 0
        self._daily_profits = []

        if self._payout_number >= MAX_PAYOUTS:
            self._status = AccountStatus.RETIRED

        return True

    def to_dict(self) -> dict:
        """Serialize full account state."""
        return {
            "account_id": self.account_id,
            "label": self.label,
            "eval_cost": str(self.eval_cost),
            "activation_cost": str(self.activation_cost),
            "group": self.group,
            "balance": str(self._balance),
            "status": self._status.value,
            "peak_balance": str(self._peak_balance),
            "liquidation_threshold": str(self._liquidation_threshold),
            "safety_net_reached": self._safety_net_reached,
            "payout_number": self._payout_number,
            "qualifying_days": self._qualifying_days,
            "total_payouts": str(self._total_payouts),
            "daily_profits": [str(d) for d in self._daily_profits],
            "tier": self.tier,
            "has_position": self.has_position,
            "dd_type": self._dd_type,
            "wave": self.wave,
        }

    # ── Private Helpers ─────────────────────────────────────────

    @staticmethod
    def _compute_pnl_points(pos: Position, price: Decimal) -> Decimal:
        """Compute P&L in NQ points."""
        if pos.direction == TradeDirection.LONG:
            return price - pos.entry_price
        return pos.entry_price - price

    def _update_trailing_dd(self, current_equity: Decimal) -> None:
        """Update peak balance and trailing drawdown threshold.

        In EOD mode this is a no-op — HWM only updates at end of day
        via update_eod_dd().
        """
        if self._dd_type == "eod":
            return
        if current_equity > self._peak_balance:
            self._peak_balance = current_equity
            if not self._safety_net_reached:
                self._liquidation_threshold = self._peak_balance - TRAILING_DD
                if self._peak_balance >= SAFETY_NET_PEAK:
                    self._safety_net_reached = True
                    self._liquidation_threshold = SAFETY_NET_LIQUIDATION

    def update_eod_dd(self) -> None:
        """Update HWM and trailing DD from end-of-day realized balance.

        Called at day boundary for EOD accounts. Same safety-net logic
        as intraday, but only triggers on closing balance, not
        unrealized equity peaks.
        """
        if self._balance > self._peak_balance:
            self._peak_balance = self._balance
            if not self._safety_net_reached:
                self._liquidation_threshold = self._peak_balance - TRAILING_DD
                if self._peak_balance >= SAFETY_NET_PEAK:
                    self._safety_net_reached = True
                    self._liquidation_threshold = SAFETY_NET_LIQUIDATION

    def _check_dll(self) -> None:
        """Check if daily loss limit is breached."""
        if self._status in (AccountStatus.BLOWN, AccountStatus.DLL_LOCKED):
            return
        if self.daily_pnl <= -self.daily_loss_limit:
            self._status = AccountStatus.DLL_LOCKED
