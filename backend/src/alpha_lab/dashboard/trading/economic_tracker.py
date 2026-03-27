"""
Economic Tracker — collects raw data during replay for Tier 1 metrics.

Observes pipeline events (trade closes, account updates, price ticks,
day boundaries) and accumulates the data needed to compute economic
viability metrics.  Metrics are computed on-demand via compute_tier1_metrics().

The tracker is only created in replay mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from alpha_lab.dashboard.trading.economic_config import EconomicConfig

logger = logging.getLogger(__name__)

# Unrealized profit milestones to track
_MILESTONES = [500.0, 1000.0, 1500.0, 2000.0, 2500.0, 3000.0]


@dataclass
class _AccountTracker:
    """Per-account tracking state collected during replay."""

    account_id: str
    starting_balance: float = 50_000.0

    # Equity history: (timestamp_iso, balance, high_water_mark, unrealized_pnl)
    equity_history: list[tuple[str, float, float, float]] = field(
        default_factory=list,
    )

    # Status progression
    blown: bool = False
    blown_at: str | None = None
    blown_balance: float | None = None
    reached_target: bool = False
    reached_target_at: str | None = None
    payout_eligible: bool = False

    # High-water marks
    peak_balance: float = 50_000.0
    peak_equity: float = 50_000.0  # balance + unrealized
    min_dd_remaining: float = 2_000.0  # closest to blow

    # Daily P&L tracking: {date_str: realized_pnl}
    daily_pnl: dict[str, float] = field(default_factory=dict)
    qualifying_days: int = 0
    current_day: str | None = None

    # Trade results for this account
    trades: list[dict] = field(default_factory=list)

    # Unrealized milestone tracking: {milestone: first_crossed_timestamp}
    # Resolved when account blows or reaches payout target.
    milestones_reached: dict[float, str] = field(default_factory=dict)
    milestones_resolved: bool = False


class EconomicTracker:
    """Collects raw economic data during replay.

    Event handlers are called from the pipeline callback chain.
    Metrics are computed on-demand, not continuously.
    """

    def __init__(self, config: EconomicConfig | None = None) -> None:
        self.config = config or EconomicConfig()
        self._accounts: dict[str, _AccountTracker] = {}
        self._total_trades: int = 0
        self._total_signals: int = 0
        self._trading_days: set[str] = set()
        self._current_date: str | None = None

        # Throttle price updates — track last update time per account
        self._last_price_ts: float = 0.0

    # ── Event handlers (called from pipeline callbacks) ────────────

    def on_trade_closed(self, trade_data: dict) -> None:
        """Record a closed trade result."""
        acct_id = trade_data.get("account_id", "")
        tracker = self._ensure_account(acct_id)

        pnl = float(trade_data.get("pnl", 0))
        pnl_points = float(trade_data.get("pnl_points", 0))
        entry_price = float(trade_data.get("entry_price", 0))
        exit_price = float(trade_data.get("exit_price", 0))
        exit_time = trade_data.get("exit_time", "")
        exit_reason = trade_data.get("exit_reason", "")

        tracker.trades.append({
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_points": pnl_points,
            "exit_reason": exit_reason,
            "exit_time": exit_time,
            "direction": trade_data.get("direction", ""),
        })

        self._total_trades += 1

        # Track unique signals by entry_time
        entry_time = trade_data.get("entry_time", "")
        if entry_time:
            # Simple count: each unique entry_time across all accounts = 1 signal
            # (tracked at metric computation time via dedup)
            pass

    def on_account_update(self, account_data: dict) -> None:
        """Track balance changes and detect blown accounts / payout milestones."""
        acct_id = account_data.get("account_id", "")
        tracker = self._ensure_account(acct_id)

        balance = float(account_data.get("balance", 50000))
        status = account_data.get("status", "active")

        # Update peak
        if balance > tracker.peak_balance:
            tracker.peak_balance = balance

        # Detect blown
        if status == "blown" and not tracker.blown:
            tracker.blown = True
            tracker.blown_at = account_data.get("timestamp", "")
            tracker.blown_balance = balance
            self._resolve_milestones(tracker, reached_payout=False)

        # Check payout eligibility milestone (balance >= min_payout_balance)
        if balance >= self.config.min_payout_balance and not tracker.reached_target:
            tracker.reached_target = True
            tracker.reached_target_at = account_data.get("timestamp", "")

    def on_price_update(
        self,
        price: float,
        timestamp: str,
        accounts: list[dict],
    ) -> None:
        """Update unrealized P&L and check milestone crossings.

        Called at most 1/sec (throttled by caller).
        accounts: list of account snapshots with balance, unrealized, etc.
        """
        for acct_data in accounts:
            acct_id = acct_data.get("account_id", "")
            tracker = self._ensure_account(acct_id)

            balance = float(acct_data.get("balance", 50000))
            unrealized = float(acct_data.get("unrealized_pnl", 0))
            equity = balance + unrealized

            # Track peak equity
            if equity > tracker.peak_equity:
                tracker.peak_equity = equity

            # Track closest to blow (DD remaining)
            # Trailing DD from peak: liquidation = peak - trailing_dd_config
            dd_remaining = equity - (tracker.peak_equity - self.config.trailing_dd)
            if dd_remaining < tracker.min_dd_remaining:
                tracker.min_dd_remaining = dd_remaining

            # Check unrealized milestones (profit from starting balance)
            unrealized_profit = equity - tracker.starting_balance
            for milestone in _MILESTONES:
                if (
                    milestone not in tracker.milestones_reached
                    and unrealized_profit >= milestone
                ):
                    tracker.milestones_reached[milestone] = timestamp

            # Equity snapshot
            tracker.equity_history.append((
                timestamp,
                balance,
                tracker.peak_balance,
                unrealized,
            ))

    def on_day_end(self, date_str: str, accounts: list[dict]) -> None:
        """End-of-day processing: qualifying days, daily P&L, consistency."""
        self._trading_days.add(date_str)

        for acct_data in accounts:
            acct_id = acct_data.get("account_id", "")
            tracker = self._ensure_account(acct_id)

            daily_pnl = float(acct_data.get("daily_pnl", 0))
            tracker.daily_pnl[date_str] = daily_pnl

            # Qualifying day: realized profit >= $200
            if daily_pnl >= 200.0:
                tracker.qualifying_days += 1

        self._current_date = date_str

    # ── Metric computation (on-demand) ─────────────────────────────

    def compute_tier1_metrics(self) -> dict:
        """Compute all Tier 1 economic metrics from collected data."""
        cfg = self.config
        accounts = list(self._accounts.values())
        n_accounts = len(accounts) or cfg.num_accounts

        # ── Payout Conversion ──────────────────────────────────────
        started = n_accounts
        reached = sum(1 for a in accounts if a.reached_target)
        blown = sum(1 for a in accounts if a.blown)
        active = sum(1 for a in accounts if not a.blown and not a.reached_target)
        conversion_rate = reached / started if started > 0 else 0

        avg_profit_at_payout = 0.0
        if reached > 0:
            profits = [
                a.peak_balance - a.starting_balance
                for a in accounts if a.reached_target
            ]
            avg_profit_at_payout = sum(profits) / len(profits)

        first_cap = cfg.payout_caps[0] if cfg.payout_caps else 0
        expected_payout = first_cap * cfg.payout_split * conversion_rate
        expected_cost = cfg.total_account_cost
        expected_net = expected_payout - expected_cost

        payout_conversion = {
            "accounts_started": started,
            "accounts_reached_target": reached,
            "accounts_blown": blown,
            "accounts_active": active,
            "payout_conversion_rate": round(conversion_rate, 4),
            "avg_profit_at_payout": round(avg_profit_at_payout, 2),
            "expected_payout_per_cycle": round(expected_payout, 2),
            "expected_cost_per_cycle": round(expected_cost, 2),
            "expected_net_per_cycle": round(expected_net, 2),
        }

        # ── Survival ───────────────────────────────────────────────
        prob_payout = conversion_rate  # Simple: reached / started
        expected_resets = (blown / started) if started > 0 else 0
        expected_cost_per_payout = (
            (cfg.total_account_cost + expected_resets * cfg.reset_cost)
            / conversion_rate
            if conversion_rate > 0
            else float("inf")
        )
        trading_days = len(self._trading_days) or 1
        expected_time = (
            trading_days / conversion_rate if conversion_rate > 0 else float("inf")
        )

        max_dd_worst = 0.0
        closest_to_blow = float("inf")
        for a in accounts:
            account_dd = a.peak_balance - (
                a.blown_balance if a.blown else min(
                    (e[1] for e in a.equity_history), default=a.starting_balance,
                )
            )
            max_dd_worst = max(max_dd_worst, account_dd)
            closest_to_blow = min(closest_to_blow, a.min_dd_remaining)

        if closest_to_blow == float("inf"):
            closest_to_blow = cfg.trailing_dd  # No data yet

        survival = {
            "prob_payout_before_ruin": round(prob_payout, 4),
            "expected_resets_per_payout": round(
                1 / conversion_rate - 1 if conversion_rate > 0 else float("inf"), 2,
            ),
            "expected_cost_per_payout": round(expected_cost_per_payout, 2),
            "expected_time_to_payout_days": round(expected_time, 1),
            "max_drawdown_worst_account": round(max_dd_worst, 2),
            "closest_to_blow": round(closest_to_blow, 2),
        }

        # ── Friction ───────────────────────────────────────────────
        total_account_costs = cfg.total_account_cost * n_accounts
        total_commissions = self._total_trades * cfg.commission_per_rt
        total_resets = blown * cfg.reset_cost
        # Trading friction: variable costs incurred during the replay
        trading_friction = total_commissions + total_resets
        # Total friction: all-in costs including initial account purchase
        total_friction = total_account_costs + trading_friction

        gross_pnl = sum(
            t["pnl"] for a in accounts for t in a.trades
        )
        # net_pnl: trading P&L minus variable friction only
        # (account setup costs are sunk and tracked separately)
        net_pnl = gross_pnl - trading_friction

        # EV economic: expected withdrawals minus ALL costs (including setup)
        total_withdrawals = reached * first_cap * cfg.payout_split
        ev_economic = total_withdrawals - total_friction

        friction = {
            "total_account_costs": round(total_account_costs, 2),
            "total_commissions": round(total_commissions, 2),
            "total_resets": round(total_resets, 2),
            "total_friction": round(total_friction, 2),
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "friction_pct": round(
                trading_friction / gross_pnl if gross_pnl > 0 else 0, 4,
            ),
            "ev_economic": round(ev_economic, 2),
        }

        # ── Conversion Rates (unrealized milestones) ───────────────
        conversion_rates = []
        for milestone in _MILESTONES:
            times_reached = sum(
                1 for a in accounts if milestone in a.milestones_reached
            )
            eventually_paid = sum(
                1 for a in accounts
                if milestone in a.milestones_reached and a.reached_target
            )
            round_tripped = sum(
                1 for a in accounts
                if milestone in a.milestones_reached and a.blown
            )
            conv_rate = eventually_paid / times_reached if times_reached > 0 else 0

            conversion_rates.append({
                "milestone": milestone,
                "times_reached": times_reached,
                "eventually_paid_out": eventually_paid,
                "round_tripped_to_ruin": round_tripped,
                "conversion_rate": round(conv_rate, 4),
            })

        # ── Throughput ─────────────────────────────────────────────
        # Deduplicate signals by entry_time across all account trades
        signal_entries: set[str] = set()
        for a in accounts:
            for t in a.trades:
                entry_time = t.get("exit_time", "")[:19]  # group by timestamp
                if entry_time:
                    signal_entries.add(
                        f"{t.get('direction', '')}_{t.get('entry_price', '')}"
                    )

        total_signals = len(signal_entries)
        signals_per_day = total_signals / trading_days if trading_days > 0 else 0
        trades_per_day = self._total_trades / trading_days if trading_days > 0 else 0

        total_payout_dollars = reached * first_cap * cfg.payout_split
        payout_per_day = total_payout_dollars / trading_days if trading_days > 0 else 0
        net_per_day = net_pnl / trading_days if trading_days > 0 else 0

        throughput = {
            "trading_days_total": trading_days,
            "signals_total": total_signals,
            "signals_per_day": round(signals_per_day, 2),
            "trades_total": self._total_trades,
            "trades_per_day": round(trades_per_day, 2),
            "payout_dollars_per_trading_day": round(payout_per_day, 2),
            "payout_dollars_per_month": round(payout_per_day * 21, 2),
            "net_profit_per_day": round(net_per_day, 2),
            "net_profit_per_month": round(net_per_day * 21, 2),
        }

        return {
            "payout_conversion": payout_conversion,
            "survival": survival,
            "friction": friction,
            "conversion_rates": conversion_rates,
            "throughput": throughput,
        }

    # ── Internal helpers ───────────────────────────────────────────

    def _ensure_account(self, account_id: str) -> _AccountTracker:
        """Get or create a tracker for the given account."""
        if account_id not in self._accounts:
            self._accounts[account_id] = _AccountTracker(
                account_id=account_id,
            )
        return self._accounts[account_id]

    def _resolve_milestones(
        self, tracker: _AccountTracker, *, reached_payout: bool,
    ) -> None:
        """Mark all open milestones as resolved (payout or ruin)."""
        tracker.milestones_resolved = True
