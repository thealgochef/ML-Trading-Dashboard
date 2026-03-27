"""
Economic configuration for Apex prop-firm analysis.

Configurable parameters for computing economic viability metrics.
Defaults match current Apex 50K Intraday PA promo pricing (March 2026).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EconomicConfig:
    """Economic parameters for prop-firm cost/benefit analysis."""

    eval_cost: float = 20.00              # Promo eval cost
    activation_cost: float = 79.00        # Activation fee
    reset_cost: float = 99.00             # $20 eval + $79 activation to replace blown account
    payout_split: float = 1.00            # 100% to trader
    trailing_dd: float = 2000.00          # $2,000 trailing DD (caps at $50,100 liquidation)
    consistency_rule_pct: float = 0.50    # Best day <= 50% of total profit
    min_trading_days: int = 5             # Minimum qualifying days before payout
    min_payout_balance: float = 52600.00  # Min balance to request first payout
    min_payout_request: float = 500.00    # Minimum payout withdrawal amount
    min_daily_profit: float = 250.00      # Min daily profit for qualifying day
    commission_per_rt: float = 7.78       # NQ round-turn cost
    num_accounts: int = 5
    max_payouts_per_account: int = 6

    # Graduated payout caps per payout number (50K account)
    payout_caps: list[float] = field(default_factory=lambda: [
        1500.00,  # 1st payout max
        2000.00,  # 2nd payout max
        2500.00,  # 3rd payout max
        2500.00,  # 4th payout max
        3000.00,  # 5th payout max
        3000.00,  # 6th payout max (final)
    ])

    @property
    def total_account_cost(self) -> float:
        """Cost to set up one account (eval + activation)."""
        return self.eval_cost + self.activation_cost

    @property
    def total_capital_at_risk(self) -> float:
        """Total upfront cost for all accounts."""
        return self.total_account_cost * self.num_accounts

    @property
    def break_even_payout_prob(self) -> float:
        """Minimum payout probability needed to break even.

        Uses first payout cap as expected withdrawal per successful account.
        P(break_even) = capital_at_risk / (num_accounts * first_payout_cap)
        """
        first_payout = self.payout_caps[0] * self.payout_split if self.payout_caps else 0
        if first_payout <= 0:
            return 1.0
        return self.total_capital_at_risk / (self.num_accounts * first_payout)

    def to_dict(self) -> dict:
        """Serialize config including computed properties."""
        return {
            "eval_cost": self.eval_cost,
            "activation_cost": self.activation_cost,
            "reset_cost": self.reset_cost,
            "payout_split": self.payout_split,
            "trailing_dd": self.trailing_dd,
            "consistency_rule_pct": self.consistency_rule_pct,
            "min_trading_days": self.min_trading_days,
            "min_payout_balance": self.min_payout_balance,
            "min_payout_request": self.min_payout_request,
            "min_daily_profit": self.min_daily_profit,
            "commission_per_rt": self.commission_per_rt,
            "num_accounts": self.num_accounts,
            "max_payouts_per_account": self.max_payouts_per_account,
            "payout_caps": self.payout_caps,
            "total_account_cost": self.total_account_cost,
            "total_capital_at_risk": self.total_capital_at_risk,
            "break_even_payout_prob": round(self.break_even_payout_prob, 4),
        }

    def update_from_dict(self, data: dict) -> None:
        """Update config fields from a dict (ignoring computed properties)."""
        settable = {
            "eval_cost", "activation_cost", "reset_cost", "payout_split",
            "trailing_dd", "consistency_rule_pct", "min_trading_days",
            "min_payout_balance", "min_payout_request", "min_daily_profit",
            "commission_per_rt", "num_accounts", "max_payouts_per_account",
        }
        for key, value in data.items():
            if key == "payout_caps" and isinstance(value, list):
                self.payout_caps = [float(v) for v in value]
            elif key in settable:
                setattr(self, key, type(getattr(self, key))(value))
