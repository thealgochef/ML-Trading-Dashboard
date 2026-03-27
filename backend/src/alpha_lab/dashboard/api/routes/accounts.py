"""
Account routes — account CRUD and payout management.

GET  /api/accounts
POST /api/accounts
GET  /api/accounts/{account_id}
POST /api/accounts/{account_id}/payout
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from alpha_lab.dashboard.api.schemas import AddAccountRequest, PayoutRequest

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _account_to_dict(acct) -> dict:
    return {
        "account_id": acct.account_id,
        "label": acct.label,
        "group": acct.group,
        "balance": float(acct.balance),
        "profit": float(acct.profit),
        "status": acct.status.value,
        "tier": acct.tier,
        "max_contracts": acct.max_contracts,
        "daily_pnl": float(acct.daily_pnl),
        "daily_loss_limit": float(acct.daily_loss_limit),
        "liquidation_threshold": float(acct.liquidation_threshold),
        "peak_balance": float(acct.peak_balance),
        "safety_net_reached": acct.safety_net_reached,
        "payout_number": acct.payout_number,
        "qualifying_days": acct.qualifying_days,
        "has_position": acct.has_position,
        "eval_cost": float(acct.eval_cost),
        "activation_cost": float(acct.activation_cost),
    }


@router.get("")
async def list_accounts(request: Request) -> dict:
    state = request.app.state.dashboard
    trades = state.todays_trades
    accounts = []
    for a in state.account_manager.get_all_accounts():
        d = _account_to_dict(a)
        d["trade_count"] = sum(
            1 for t in trades if t.get("account_id") == a.account_id
        )
        accounts.append(d)
    summary = state.account_manager.get_portfolio_summary()
    # Convert Decimals in summary to float for JSON
    summary_json = {k: float(v) if isinstance(v, Decimal) else v for k, v in summary.items()}
    return {"accounts": accounts, "summary": summary_json}


@router.post("")
async def add_account(body: AddAccountRequest, request: Request) -> dict:
    state = request.app.state.dashboard
    acct = state.account_manager.add_account(
        label=body.label,
        eval_cost=Decimal(str(body.eval_cost)),
        activation_cost=Decimal(str(body.activation_cost)),
        group=body.group,
    )
    return {"account": _account_to_dict(acct)}


@router.get("/{account_id}")
async def get_account(account_id: str, request: Request) -> dict:
    state = request.app.state.dashboard
    acct = state.account_manager.get_account(account_id)
    if acct is None:
        return JSONResponse(
            status_code=404, content={"error": f"Account {account_id} not found"},
        )
    # Filter trades for this account
    trade_history = [
        t for t in state.todays_trades if t.get("account_id") == account_id
    ]
    return {"account": _account_to_dict(acct), "trade_history": trade_history}


@router.post("/{account_id}/payout")
async def request_payout(
    account_id: str, body: PayoutRequest, request: Request,
) -> dict:
    state = request.app.state.dashboard
    acct = state.account_manager.get_account(account_id)
    if acct is None:
        return JSONResponse(
            status_code=404, content={"error": f"Account {account_id} not found"},
        )
    result = acct.request_payout(Decimal(str(body.amount)))
    if not result:
        return JSONResponse(
            status_code=400, content={"error": "Payout rejected — eligibility not met"},
        )
    return {
        "payout": {
            "account_id": account_id,
            "amount": body.amount,
            "new_balance": float(acct.balance),
            "payout_number": acct.payout_number,
        },
    }
