"""
Account Manager — multi-account portfolio management.

Manages the portfolio of simulated Apex 4.0 accounts. Groups accounts
into Group A and Group B, handles adding/replacing accounts, and
provides aggregate portfolio statistics.
"""

from __future__ import annotations

from decimal import Decimal

from alpha_lab.dashboard.trading import AccountStatus
from alpha_lab.dashboard.trading.apex_account import ApexAccount


class AccountManager:
    """Manages the portfolio of simulated Apex 4.0 accounts.

    Organizes accounts into Group A (smaller TP, consistency builders)
    and Group B (larger TP, runners). Handles adding new accounts,
    replacing blown/retired ones, and aggregate portfolio stats.
    """

    def __init__(self) -> None:
        self._accounts: dict[str, ApexAccount] = {}
        self._next_id = 1

    def add_account(
        self,
        label: str,
        eval_cost: Decimal,
        activation_cost: Decimal,
        group: str,
        dd_type: str = "intraday",
    ) -> ApexAccount:
        """Add a new account to the portfolio."""
        account_id = f"APEX-{self._next_id:03d}"
        self._next_id += 1

        acct = ApexAccount(
            account_id=account_id,
            label=label,
            eval_cost=eval_cost,
            activation_cost=activation_cost,
            group=group,
            dd_type=dd_type,
        )
        self._accounts[account_id] = acct
        return acct

    def get_active_accounts(self) -> list[ApexAccount]:
        """Return accounts with ACTIVE or DLL_LOCKED status."""
        return [
            a for a in self._accounts.values()
            if a.status in (AccountStatus.ACTIVE, AccountStatus.DLL_LOCKED)
        ]

    def get_tradeable_accounts(self) -> list[ApexAccount]:
        """Return accounts eligible for new trades.

        Must be ACTIVE (not DLL_LOCKED), and have no open position.
        """
        return [
            a for a in self._accounts.values()
            if a.status == AccountStatus.ACTIVE and not a.has_position
        ]

    def get_accounts_by_group(self, group: str) -> list[ApexAccount]:
        """Return all accounts in a group (any status)."""
        return [
            a for a in self._accounts.values()
            if a.group == group
        ]

    def get_all_accounts(self) -> list[ApexAccount]:
        """Return all accounts including blown and retired."""
        return list(self._accounts.values())

    def get_account(self, account_id: str) -> ApexAccount | None:
        """Get a specific account by ID."""
        return self._accounts.get(account_id)

    def get_portfolio_summary(self) -> dict:
        """Aggregate portfolio stats."""
        all_accts = list(self._accounts.values())

        total_invested = sum(
            a.eval_cost + a.activation_cost for a in all_accts
        )
        total_balance = sum(a.balance for a in all_accts)
        total_profit = sum(a.profit for a in all_accts)
        total_payouts = sum(a._total_payouts for a in all_accts)
        active_count = sum(
            1 for a in all_accts
            if a.status in (AccountStatus.ACTIVE, AccountStatus.DLL_LOCKED)
        )

        return {
            "total_invested": total_invested,
            "total_balance": total_balance,
            "total_profit": total_profit,
            "total_payouts": total_payouts,
            "active_count": active_count,
            "total_accounts": len(all_accts),
            "blown_count": sum(
                1 for a in all_accts if a.status == AccountStatus.BLOWN
            ),
            "retired_count": sum(
                1 for a in all_accts if a.status == AccountStatus.RETIRED
            ),
        }

    def start_new_day(self) -> None:
        """Reset daily counters for all active accounts."""
        for acct in self._accounts.values():
            if acct.status in (AccountStatus.ACTIVE, AccountStatus.DLL_LOCKED):
                acct.start_new_day()

    def save_state(self) -> list[dict]:
        """Serialize all account state for persistence."""
        return [acct.to_dict() for acct in self._accounts.values()]

    def load_state(self, data: list[dict]) -> None:
        """Restore accounts from serialized state."""
        self._accounts.clear()
        for record in data:
            acct = ApexAccount(
                account_id=record["account_id"],
                label=record["label"],
                eval_cost=Decimal(record["eval_cost"]),
                activation_cost=Decimal(record["activation_cost"]),
                group=record["group"],
                dd_type=record.get("dd_type", "intraday"),
            )
            # Restore internal state
            acct._balance = Decimal(record["balance"])
            acct._status = AccountStatus(record["status"])
            acct._peak_balance = Decimal(record["peak_balance"])
            acct._liquidation_threshold = Decimal(record["liquidation_threshold"])
            acct._safety_net_reached = record["safety_net_reached"]
            acct._payout_number = record["payout_number"]
            acct._qualifying_days = record["qualifying_days"]
            acct._total_payouts = Decimal(record["total_payouts"])
            acct._daily_profits = [
                Decimal(d) for d in record.get("daily_profits", [])
            ]
            acct.wave = record.get("wave")

            self._accounts[acct.account_id] = acct

            # Track next ID
            num = int(acct.account_id.split("-")[1])
            if num >= self._next_id:
                self._next_id = num + 1
