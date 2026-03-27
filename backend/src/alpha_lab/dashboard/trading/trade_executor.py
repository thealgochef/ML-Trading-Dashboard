"""
Trade Executor — trade placement across simulated Apex accounts.

When the prediction engine produces an executable signal (reversal
during NY RTH), places trades on all eligible accounts simultaneously.
Manages the second-signal mode (ignore or flip) and the no-hedging
constraint.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.trading import ClosedTrade, Position
from alpha_lab.dashboard.trading.account_manager import AccountManager

ET = ZoneInfo("America/New_York")

# No new trades at or after 3:55 PM ET (flatten time)
_FLATTEN_HOUR_ET = 15
_FLATTEN_MINUTE = 55


class TradeExecutor:
    """Executes paper trades across simulated Apex accounts.

    Enforces the no-hedging constraint: at no point can one account be
    long while another is short.
    """

    def __init__(self, account_manager: AccountManager) -> None:
        self._mgr = account_manager
        self._open_callbacks: list[Callable[[Position], None]] = []
        self._close_callbacks: list[Callable[[ClosedTrade], None]] = []
        self.second_signal_mode: str = "ignore"  # "ignore" or "flip"

    def on_prediction(
        self,
        prediction: dict,
        timestamp: datetime,
        current_price: Decimal | None = None,
    ) -> list[Position]:
        """Handle an executable prediction. Opens positions on eligible accounts.

        Entry is at current_price (market price when prediction fires),
        NOT at the level/touch price from 5 minutes earlier.
        Rejects trades at or after 3:55 PM ET (flatten time).
        """
        if not prediction.get("is_executable", False):
            logger.info("Trade blocked: prediction not executable")
            return []

        # No new trades at or after flatten time
        ts_et = timestamp.astimezone(ET)
        if (ts_et.hour > _FLATTEN_HOUR_ET
                or (ts_et.hour == _FLATTEN_HOUR_ET
                    and ts_et.minute >= _FLATTEN_MINUTE)):
            logger.info("Trade blocked: past flatten time (%s ET)", ts_et.strftime("%H:%M"))
            return []

        direction: TradeDirection = prediction["trade_direction"]
        # Use current market price for entry, not the level price
        entry_price: Decimal = current_price if current_price is not None else prediction["level_price"]

        # No-hedging check: if any account has a position in opposite direction
        if self._has_conflicting_position(direction):
            if self.second_signal_mode == "ignore":
                logger.info(
                    "Trade blocked: conflicting position exists, mode=ignore, direction=%s",
                    direction.value,
                )
                return []
            if self.second_signal_mode == "flip":
                logger.info("Trade flip: closing conflicting positions before opening %s", direction.value)
                self.close_all_positions(entry_price, "flip", timestamp)

        eligible = self._mgr.get_tradeable_accounts()
        opened: list[Position] = []

        for acct in eligible:
            contracts = min(1, acct.max_contracts)
            pos = acct.open_position(direction, entry_price, contracts, timestamp)
            opened.append(pos)
            for cb in self._open_callbacks:
                cb(pos)

        logger.info(
            "Trade executed: direction=%s, entry=%.2f, accounts=%d",
            direction.value, float(entry_price), len(opened),
        )
        return opened

    def close_all_positions(
        self,
        current_price: Decimal,
        reason: str,
        exit_time: datetime,
    ) -> list[ClosedTrade]:
        """Close all positions across all accounts."""
        trades: list[ClosedTrade] = []

        for acct in self._mgr.get_all_accounts():
            if acct.has_position:
                trade = acct.close_position(current_price, reason, exit_time)
                trades.append(trade)
                for cb in self._close_callbacks:
                    cb(trade)

        return trades

    def close_account_position(
        self,
        account_id: str,
        current_price: Decimal,
        reason: str,
        exit_time: datetime,
    ) -> ClosedTrade | None:
        """Close position on a single account."""
        acct = self._mgr.get_account(account_id)
        if acct is None or not acct.has_position:
            return None

        trade = acct.close_position(current_price, reason, exit_time)
        for cb in self._close_callbacks:
            cb(trade)
        return trade

    def manual_entry(
        self,
        account_id: str,
        direction: TradeDirection,
        price: Decimal,
        timestamp: datetime,
    ) -> Position | None:
        """Manual trade entry on a specific account."""
        acct = self._mgr.get_account(account_id)
        if acct is None or acct.has_position:
            return None

        pos = acct.open_position(direction, price, 1, timestamp)
        for cb in self._open_callbacks:
            cb(pos)
        return pos

    def hard_flatten(
        self,
        current_price: Decimal,
        flatten_time: datetime,
    ) -> list[ClosedTrade]:
        """3:55 PM CT hard flatten. Close ALL positions, no exceptions."""
        return self.close_all_positions(current_price, "flatten", flatten_time)

    def on_trade_opened(self, callback: Callable[[Position], None]) -> None:
        """Register callback for opened positions."""
        self._open_callbacks.append(callback)

    def on_trade_closed(self, callback: Callable[[ClosedTrade], None]) -> None:
        """Register callback for closed trades."""
        self._close_callbacks.append(callback)

    def _has_conflicting_position(self, direction: TradeDirection) -> bool:
        """Check if any account has a position in the opposite direction."""
        for acct in self._mgr.get_all_accounts():
            if acct.has_position and acct.current_position.direction != direction:
                return True
        return False
