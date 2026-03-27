"""
Position Monitor — real-time TP/SL/DLL/flatten enforcement.

Processes every trade tick against all open positions. Detects TP hits,
SL hits, DLL breaches, trailing drawdown violations, and the hard
3:55 PM CT flatten.

With 5 accounts and 1 position each, checking all on every tick is
fine. No batching or sampling needed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.trading import (
    NQ_POINT_VALUE,
    AccountStatus,
    ClosedTrade,
)
from alpha_lab.dashboard.trading.account_manager import AccountManager
from alpha_lab.dashboard.trading.trade_executor import TradeExecutor

ET = ZoneInfo("America/New_York")

# Hard flatten at 3:55 PM ET (15:55 ET), non-configurable.
# This uses ET timezone conversion (handles EST/EDT automatically).
FLATTEN_HOUR_ET = 15
FLATTEN_MINUTE = 55


class PositionMonitor:
    """Monitors open positions and enforces TP/SL/DLL/flatten rules.

    Processes every trade tick against all open positions.
    """

    def __init__(
        self,
        account_manager: AccountManager,
        trade_executor: TradeExecutor,
    ) -> None:
        self._mgr = account_manager
        self._executor = trade_executor

        # Default TP/SL per group (1:1 R:R)
        self._group_tp: dict[str, Decimal] = {
            "A": Decimal("15"),
            "B": Decimal("30"),
        }
        self._group_sl: dict[str, Decimal] = {
            "A": Decimal("15"),
            "B": Decimal("30"),
        }

        # Per-account TP/SL overrides (takes priority over group-level)
        self._account_tp: dict[str, Decimal] = {}
        self._account_sl: dict[str, Decimal] = {}

    def on_trade(self, trade) -> list[ClosedTrade]:
        """Process a tick against all open positions.

        Returns list of any closed trades.
        """
        closed: list[ClosedTrade] = []
        trade_price = trade.price
        trade_ts = trade.timestamp

        for acct in self._mgr.get_all_accounts():
            if not acct.has_position:
                continue
            if acct.status == AccountStatus.BLOWN:
                continue

            # 1. Update unrealized P&L, MFE/MAE, trailing DD
            acct.update_unrealized(trade_price)

            # 2. Check if account was blown by update_unrealized
            if acct.status == AccountStatus.BLOWN:
                if acct.has_position:
                    trade_result = self._executor.close_account_position(
                        acct.account_id, trade_price, "blown", trade_ts,
                    )
                    if trade_result is not None:
                        logger.info(
                            "Position closed: account=%s, reason=blown, pnl=%.2f",
                            acct.account_id, float(trade_result.pnl),
                        )
                        closed.append(trade_result)
                continue

            # 3. Check DLL breach (update_unrealized may have locked it)
            if acct.status == AccountStatus.DLL_LOCKED:
                if acct.has_position:
                    trade_result = self._executor.close_account_position(
                        acct.account_id, trade_price, "dll", trade_ts,
                    )
                    if trade_result is not None:
                        logger.info(
                            "Position closed: account=%s, reason=dll, pnl=%.2f",
                            acct.account_id, float(trade_result.pnl),
                        )
                        closed.append(trade_result)
                continue

            # 4. Check TP/SL
            pos = acct.current_position
            tp_points = self._account_tp.get(
                acct.account_id,
                self._group_tp.get(acct.group, Decimal("15")),
            )
            sl_points = self._account_sl.get(
                acct.account_id,
                self._group_sl.get(acct.group, Decimal("15")),
            )

            tp_target = tp_points * NQ_POINT_VALUE * pos.contracts
            sl_target = sl_points * NQ_POINT_VALUE * pos.contracts

            if pos.unrealized_pnl >= tp_target:
                # Exit at exact TP price, not market price
                if pos.direction == TradeDirection.LONG:
                    tp_exit = pos.entry_price + tp_points
                else:
                    tp_exit = pos.entry_price - tp_points
                trade_result = self._executor.close_account_position(
                    acct.account_id, tp_exit, "tp", trade_ts,
                )
                if trade_result is not None:
                    logger.info(
                        "Position closed: account=%s, reason=tp, pnl=%.2f, entry=%.2f, exit=%.2f",
                        acct.account_id, float(trade_result.pnl),
                        float(trade_result.entry_price), float(tp_exit),
                    )
                    closed.append(trade_result)
            elif pos.unrealized_pnl <= -sl_target:
                # Exit at exact SL price, not market price
                if pos.direction == TradeDirection.LONG:
                    sl_exit = pos.entry_price - sl_points
                else:
                    sl_exit = pos.entry_price + sl_points
                trade_result = self._executor.close_account_position(
                    acct.account_id, sl_exit, "sl", trade_ts,
                )
                if trade_result is not None:
                    logger.info(
                        "Position closed: account=%s, reason=sl, pnl=%.2f, entry=%.2f, exit=%.2f",
                        acct.account_id, float(trade_result.pnl),
                        float(trade_result.entry_price), float(sl_exit),
                    )
                    closed.append(trade_result)

        return closed

    def check_flatten_time(
        self,
        current_time: datetime,
        current_price: Decimal,
    ) -> list[ClosedTrade]:
        """Check if hard flatten time reached. Close all if so.

        3:55 PM ET, non-configurable. Uses ET timezone conversion
        so EST/EDT is handled automatically.
        """
        ts_et = current_time.astimezone(ET)
        if (
            ts_et.hour > FLATTEN_HOUR_ET
            or (
                ts_et.hour == FLATTEN_HOUR_ET
                and ts_et.minute >= FLATTEN_MINUTE
            )
        ):
            closed = self._executor.hard_flatten(current_price, current_time)
            if closed:
                logger.info(
                    "Flatten triggered: time=%s ET, positions_closed=%d",
                    ts_et.strftime("%H:%M"), len(closed),
                )
            return closed
        return []

    def get_group_tp(self, group: str) -> Decimal:
        """Get TP for a group in points."""
        return self._group_tp.get(group, Decimal("15"))

    def set_group_tp(self, group: str, points: Decimal) -> None:
        """Set TP for a group in points."""
        self._group_tp[group] = points

    def get_group_sl(self, group: str) -> Decimal:
        """Get SL for a group in points."""
        return self._group_sl.get(group, Decimal("15"))

    def set_group_sl(self, group: str, points: Decimal) -> None:
        """Set SL for a group in points."""
        self._group_sl[group] = points

    # ── Per-account TP/SL overrides ────────────────────────────

    def set_account_tp(self, account_id: str, points: Decimal) -> None:
        """Set TP override for a specific account."""
        self._account_tp[account_id] = points

    def set_account_sl(self, account_id: str, points: Decimal) -> None:
        """Set SL override for a specific account."""
        self._account_sl[account_id] = points

    def clear_account_tp_sl(self, account_id: str) -> None:
        """Remove per-account TP/SL overrides, falling back to group-level."""
        self._account_tp.pop(account_id, None)
        self._account_sl.pop(account_id, None)
