"""
Regime-Wave Executor — per-account regime/wave adaptive trade execution.

Evaluates each account independently through a regime + wave decision
flowchart.  Regime (Survival/Sprint/Compound/Harvest) determines TP/SL.
Wave (Scout/Confirmer/Sniper) determines entry timing.

Used by Strategies C and D in the comparison framework.  Strategy D also
enables EOD compounding (extra trades using the frozen-HWM free buffer).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.model import Prediction
from alpha_lab.dashboard.trading import (
    NQ_POINT_VALUE,
    PAYOUT_CAPS,
    SAFETY_NET_PEAK,
    AccountStatus,
    ClosedTrade,
    Position,
)
from alpha_lab.dashboard.trading.account_manager import AccountManager
from alpha_lab.dashboard.trading.apex_account import ApexAccount
from alpha_lab.dashboard.trading.position_monitor import PositionMonitor

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# No new trades at or after 3:55 PM ET
_FLATTEN_HOUR_ET = 15
_FLATTEN_MINUTE = 55

# Default sniper confidence threshold
_DEFAULT_SNIPER_CONFIDENCE = 0.70

# Compound confidence discount per prior win
_COMPOUND_CONFIDENCE_DISCOUNT = Decimal("0.10")

# Confirmation offset from signal price (NQ points)
_CONFIRMER_OFFSET = Decimal("3")


class Regime(Enum):
    SURVIVAL = "survival"    # balance < $49,200: TP=15, SL=15
    SPRINT = "sprint"        # $49,200 - $52,100: TP=15, SL=30
    COMPOUND = "compound"    # $52,100 - payout_threshold: TP=15, SL=30
    HARVEST = "harvest"      # >= payout_threshold: TP=15, SL=30


# Regime TP/SL in NQ points
_REGIME_TP: dict[Regime, Decimal] = {
    Regime.SURVIVAL: Decimal("15"),
    Regime.SPRINT: Decimal("15"),
    Regime.COMPOUND: Decimal("15"),
    Regime.HARVEST: Decimal("15"),
}
_REGIME_SL: dict[Regime, Decimal] = {
    Regime.SURVIVAL: Decimal("15"),
    Regime.SPRINT: Decimal("30"),
    Regime.COMPOUND: Decimal("30"),
    Regime.HARVEST: Decimal("30"),
}

# Regime balance boundaries
_SURVIVAL_CEILING = Decimal("49200")
_SPRINT_CEILING = SAFETY_NET_PEAK  # $52,100


@dataclass
class PendingConfirmation:
    """A pending confirmer limit-order awaiting fill."""

    account_id: str
    direction: TradeDirection
    confirmation_price: Decimal  # signal_price ± 3 pts
    signal_timestamp: datetime
    scouts_resolved: bool = False  # True → cancel this pending


class RegimeWaveExecutor:
    """Regime/wave adaptive executor for Strategies C and D.

    Opens positions directly via ApexAccount.open_position() and sets
    per-account TP/SL on the shared PositionMonitor.
    """

    def __init__(
        self,
        account_manager: AccountManager,
        position_monitor: PositionMonitor,
        enable_eod_compounding: bool = False,
    ) -> None:
        self._mgr = account_manager
        self._monitor = position_monitor
        self._enable_compounding = enable_eod_compounding

        # Per-account state
        self._parked: set[str] = set()
        self._pending: dict[str, PendingConfirmation] = {}
        self._daily_trade_count: dict[str, int] = {}
        self._day_start_balances: dict[str, Decimal] = {}

        # Compounding state
        self._compound_eligible: set[str] = set()  # accounts with buffer for compound
        self._compound_trades_today: dict[str, int] = {}
        self._compound_stopped: set[str] = set()
        self._confidence_threshold: dict[str, float] = {}

        # Track which accounts opened via compound (for on_trade_closed)
        self._compound_entries: set[str] = set()

        # Callbacks
        self._close_callbacks: list = []

        # Stats
        self.stats = {
            "confirmer_fills": 0,
            "confirmer_cancels": 0,
            "compound_trades": 0,
            "compound_wins": 0,
        }

    # ── Regime determination ──────────────────────────────────────

    def _get_regime(self, acct: ApexAccount) -> Regime:
        """Determine regime from balance and payout state."""
        balance = acct.balance
        payout_threshold = self._payout_threshold(acct)

        if balance >= payout_threshold:
            return Regime.HARVEST
        if balance >= _SPRINT_CEILING:
            return Regime.COMPOUND
        if balance >= _SURVIVAL_CEILING:
            return Regime.SPRINT
        return Regime.SURVIVAL

    @staticmethod
    def _payout_threshold(acct: ApexAccount) -> Decimal:
        """Minimum balance for payout eligibility.

        safety_net_peak + payout_cap[payout_number]
        """
        payout_num = acct.payout_number
        if payout_num >= len(PAYOUT_CAPS):
            return Decimal("999999")  # Already maxed out
        return SAFETY_NET_PEAK + PAYOUT_CAPS[payout_num]

    # ── Core event handlers ───────────────────────────────────────

    def on_prediction(
        self,
        prediction: Prediction,
        current_price: Decimal,
        timestamp: datetime,
    ) -> list[Position]:
        """Handle a prediction. Evaluates each account independently.

        Returns list of positions opened immediately (scouts + snipers).
        Confirmers are queued as pending orders.
        """
        if not prediction.is_executable:
            return []

        # No new trades at or after flatten time
        ts_et = timestamp.astimezone(ET)
        if (ts_et.hour > _FLATTEN_HOUR_ET
                or (ts_et.hour == _FLATTEN_HOUR_ET
                    and ts_et.minute >= _FLATTEN_MINUTE)):
            return []

        direction = prediction.trade_direction
        opened: list[Position] = []

        for acct in self._mgr.get_all_accounts():
            if not self._is_eligible(acct):
                continue

            regime = self._get_regime(acct)
            wave = acct.wave

            # Set regime-based TP/SL for this account
            tp = _REGIME_TP[regime]
            sl = _REGIME_SL[regime]
            self._monitor.set_account_tp(acct.account_id, tp)
            self._monitor.set_account_sl(acct.account_id, sl)

            # Harvest filter: max 1 trade/day, confidence >= 70%
            if regime == Regime.HARVEST:
                if self._daily_trade_count.get(acct.account_id, 0) >= 1:
                    continue
                reversal_prob = prediction.probabilities.get(
                    "tradeable_reversal", 0.0,
                )
                if reversal_prob < _DEFAULT_SNIPER_CONFIDENCE:
                    continue

            # Compound-eligible accounts use lowered confidence threshold
            # and are tagged as compound entries when they trade
            is_compound = acct.account_id in self._compound_eligible

            # Wave dispatch
            if wave == "scout":
                pos = self._enter_trade(acct, direction, current_price, timestamp)
                if pos:
                    if is_compound:
                        self._mark_compound_entry(acct.account_id)
                    opened.append(pos)

            elif wave == "confirmer":
                # Queue pending order at signal ± 3pts
                if direction == TradeDirection.LONG:
                    confirm_price = current_price - _CONFIRMER_OFFSET
                else:
                    confirm_price = current_price + _CONFIRMER_OFFSET
                self._pending[acct.account_id] = PendingConfirmation(
                    account_id=acct.account_id,
                    direction=direction,
                    confirmation_price=confirm_price,
                    signal_timestamp=timestamp,
                )

            elif wave == "sniper":
                threshold = self._confidence_threshold.get(
                    acct.account_id, _DEFAULT_SNIPER_CONFIDENCE,
                )
                reversal_prob = prediction.probabilities.get(
                    "tradeable_reversal", 0.0,
                )
                if reversal_prob >= threshold:
                    pos = self._enter_trade(acct, direction, current_price, timestamp)
                    if pos:
                        if is_compound:
                            self._mark_compound_entry(acct.account_id)
                        opened.append(pos)

        return opened

    def on_tick(
        self,
        price: Decimal,
        timestamp: datetime,
    ) -> list[Position]:
        """Process pending confirmer orders on each tick.

        Returns list of newly opened positions from confirmer fills.
        """
        filled: list[Position] = []
        to_remove: list[str] = []

        for acct_id, pending in self._pending.items():
            acct = self._mgr.get_account(acct_id)

            # Cancel if scouts already resolved
            if pending.scouts_resolved:
                to_remove.append(acct_id)
                self.stats["confirmer_cancels"] += 1
                continue

            # Cancel if account no longer eligible
            if acct is None or not self._is_eligible(acct):
                to_remove.append(acct_id)
                self.stats["confirmer_cancels"] += 1
                continue

            # Check fill: price reaches confirmation level
            filled_now = False
            if pending.direction == TradeDirection.LONG:
                if price <= pending.confirmation_price:
                    filled_now = True
            else:
                if price >= pending.confirmation_price:
                    filled_now = True

            if filled_now:
                # Enter at confirmation price, not current price
                is_compound = acct_id in self._compound_eligible
                pos = self._enter_trade(
                    acct, pending.direction,
                    pending.confirmation_price, timestamp,
                )
                if pos:
                    if is_compound:
                        self._mark_compound_entry(acct_id)
                    filled.append(pos)
                    self.stats["confirmer_fills"] += 1
                to_remove.append(acct_id)

        for acct_id in to_remove:
            self._pending.pop(acct_id, None)

        return filled

    def on_trade_closed(self, trade: ClosedTrade) -> None:
        """Called when any position closes (from PositionMonitor).

        1. If scout closed → set scouts_resolved on pending confirmers.
        2. If win + compounding → check compound opportunity.
        3. If compound loss → stop compounding for this account.
        4. Check parking (Harvest + qualifying days).
        """
        acct = self._mgr.get_account(trade.account_id)
        if acct is None:
            return

        # 1. Scout resolution: cancel pending confirmers
        if acct.wave == "scout":
            for pending in self._pending.values():
                pending.scouts_resolved = True

        # 2. Compounding logic
        is_compound_trade = trade.account_id in self._compound_entries
        if is_compound_trade:
            self._compound_entries.discard(trade.account_id)
            self._compound_eligible.discard(trade.account_id)
            if trade.pnl > 0:
                self.stats["compound_wins"] += 1
            else:
                # Compound loss → stop compounding for this account today
                self._compound_stopped.add(trade.account_id)

        # After any win, check if account becomes compound-eligible
        if trade.pnl > 0 and self._enable_compounding:
            self._check_compound_eligibility(acct)

        # 3. Check parking: Harvest regime + qualifying days >= 5
        regime = self._get_regime(acct)
        if regime == Regime.HARVEST and acct.qualifying_days >= 5:
            self._parked.add(acct.account_id)

    def check_payouts(self) -> list[tuple[str, Decimal]]:
        """Check payout eligibility and execute payouts.

        Called at day boundary. Returns list of (account_id, amount) paid out.
        """
        payouts: list[tuple[str, Decimal]] = []

        for acct in self._mgr.get_all_accounts():
            if not acct.payout_eligible:
                continue

            # Payout amount: min(balance - safety_net_liquidation, payout_cap)
            available = acct.balance - Decimal("52100")
            cap = acct.max_payout_amount
            amount = min(available, cap)

            if amount < Decimal("500"):
                continue

            if acct.request_payout(amount):
                payouts.append((acct.account_id, amount))
                # Unpark after payout
                self._parked.discard(acct.account_id)
                logger.info(
                    "Payout: account=%s, amount=$%.2f, payout_number=%d",
                    acct.account_id, float(amount), acct.payout_number,
                )

        return payouts

    def end_day(self) -> None:
        """End-of-day processing. Call BEFORE account_manager.start_new_day()."""
        for acct in self._mgr.get_all_accounts():
            if acct.status in (AccountStatus.ACTIVE, AccountStatus.DLL_LOCKED):
                acct.end_day()

    def start_new_day(self) -> None:
        """Reset daily state. Call AFTER account_manager.start_new_day()."""
        self._pending.clear()
        self._daily_trade_count.clear()
        self._compound_eligible.clear()
        self._compound_trades_today.clear()
        self._compound_stopped.clear()
        self._compound_entries.clear()

        # Reset confidence thresholds
        self._confidence_threshold.clear()

        # Snapshot day-start balances (for EOD free buffer calc)
        for acct in self._mgr.get_all_accounts():
            self._day_start_balances[acct.account_id] = acct.balance

        # Re-evaluate parking
        self._parked.clear()
        for acct in self._mgr.get_all_accounts():
            regime = self._get_regime(acct)
            if regime == Regime.HARVEST and acct.qualifying_days >= 5:
                self._parked.add(acct.account_id)

    # ── Private helpers ───────────────────────────────────────────

    def _is_eligible(self, acct: ApexAccount) -> bool:
        """Check if account is eligible for a new trade."""
        if acct.status != AccountStatus.ACTIVE:
            return False
        if acct.has_position:
            return False
        if acct.account_id in self._parked:
            return False
        return True

    def _enter_trade(
        self,
        acct: ApexAccount,
        direction: TradeDirection,
        price: Decimal,
        timestamp: datetime,
    ) -> Position | None:
        """Open a position on an account. Returns None on failure."""
        try:
            contracts = min(1, acct.max_contracts)
            pos = acct.open_position(direction, price, contracts, timestamp)

            # Track daily trade count
            self._daily_trade_count[acct.account_id] = (
                self._daily_trade_count.get(acct.account_id, 0) + 1
            )

            logger.debug(
                "RW entry: account=%s, wave=%s, regime=%s, direction=%s, price=%.2f",
                acct.account_id, acct.wave,
                self._get_regime(acct).value, direction.value, float(price),
            )
            return pos
        except ValueError as e:
            logger.warning("RW entry failed: %s", e)
            return None

    def _mark_compound_entry(self, account_id: str) -> None:
        """Tag an account's current trade as a compound entry.

        Called from on_prediction() / on_tick() when a compound-eligible
        account enters a real signal.
        """
        self._compound_entries.add(account_id)
        self._compound_eligible.discard(account_id)
        self._compound_trades_today[account_id] = (
            self._compound_trades_today.get(account_id, 0) + 1
        )
        self.stats["compound_trades"] += 1

    def _check_compound_eligibility(self, acct: ApexAccount) -> None:
        """Check if account qualifies for compound on the NEXT signal.

        Sets ``_compound_eligible`` flag and lowers sniper confidence
        threshold.  Does NOT open a trade — compound fires on the next
        real prediction via on_prediction() / on_tick().
        """
        if not self._enable_compounding:
            return
        if acct.account_id in self._compound_stopped:
            return
        if self._compound_trades_today.get(acct.account_id, 0) >= 2:
            return
        if acct.status != AccountStatus.ACTIVE:
            return

        regime = self._get_regime(acct)

        # Harvest blocks extra trades (max 1/day already enforced)
        if regime == Regime.HARVEST:
            return

        sl = _REGIME_SL[regime]
        sl_dollars = sl * NQ_POINT_VALUE  # 1 contract

        # Total buffer: distance from liquidation must absorb a potential SL loss
        total_buffer = acct.balance - acct.liquidation_threshold
        if total_buffer < sl_dollars:
            return

        # DLL check
        if acct.dll_remaining < sl_dollars:
            return

        # Mark eligible — will fire on next signal via on_prediction()
        self._compound_eligible.add(acct.account_id)

        # Lower sniper threshold by 10% for this account
        current_threshold = self._confidence_threshold.get(
            acct.account_id, _DEFAULT_SNIPER_CONFIDENCE,
        )
        self._confidence_threshold[acct.account_id] = max(
            0.30, current_threshold - float(_COMPOUND_CONFIDENCE_DISCOUNT),
        )

        logger.info(
            "Compound eligible: account=%s, total_buffer=$%.2f, "
            "new_threshold=%.2f",
            acct.account_id, float(total_buffer),
            self._confidence_threshold[acct.account_id],
        )
