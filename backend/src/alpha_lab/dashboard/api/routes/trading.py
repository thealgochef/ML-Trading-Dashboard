"""
Trading routes — manual trade actions and position management.

POST /api/trading/close-all
POST /api/trading/close/{account_id}
POST /api/trading/manual-entry
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from alpha_lab.dashboard.api.schemas import (
    CloseAccountRequest,
    CloseAllRequest,
    ManualEntryRequest,
)
from alpha_lab.dashboard.engine.models import TradeDirection

router = APIRouter(prefix="/api/trading", tags=["trading"])


def _closed_trade_to_dict(trade) -> dict:
    return {
        "account_id": trade.account_id,
        "direction": trade.direction.value,
        "entry_price": float(trade.entry_price),
        "exit_price": float(trade.exit_price),
        "contracts": trade.contracts,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "pnl": float(trade.pnl),
        "pnl_points": float(trade.pnl_points),
        "exit_reason": trade.exit_reason,
        "group": trade.group,
    }


@router.post("/close-all")
async def close_all_positions(body: CloseAllRequest, request: Request) -> dict:
    state = request.app.state.dashboard
    now = datetime.now(UTC)
    price = Decimal(str(state.latest_price)) if state.latest_price else Decimal("0")
    closed = state.trade_executor.close_all_positions(price, body.reason, now)
    return {"closed_trades": [_closed_trade_to_dict(t) for t in closed]}


@router.post("/close/{account_id}")
async def close_account_position(
    account_id: str, body: CloseAccountRequest, request: Request,
) -> dict:
    state = request.app.state.dashboard
    now = datetime.now(UTC)
    price = Decimal(str(state.latest_price)) if state.latest_price else Decimal("0")
    trade = state.trade_executor.close_account_position(
        account_id, price, body.reason, now,
    )
    if trade is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"No open position on {account_id}"},
        )
    return {"closed_trade": _closed_trade_to_dict(trade)}


@router.post("/manual-entry")
async def manual_entry(body: ManualEntryRequest, request: Request) -> dict:
    """Open positions on ALL tradeable accounts at current market price."""
    state = request.app.state.dashboard
    direction = TradeDirection.LONG if body.direction == "long" else TradeDirection.SHORT
    now = datetime.now(UTC)

    if state.latest_price is None:
        return JSONResponse(
            status_code=400,
            content={"error": "No market price available yet"},
        )

    price = Decimal(str(state.latest_price))
    eligible = state.account_manager.get_tradeable_accounts()

    if not eligible:
        return JSONResponse(
            status_code=400,
            content={"error": "No tradeable accounts available"},
        )

    opened = []
    for acct in eligible:
        pos = state.trade_executor.manual_entry(
            acct.account_id, direction, price, now,
        )
        if pos is not None:
            opened.append({
                "account_id": pos.account_id,
                "direction": pos.direction.value,
                "entry_price": float(pos.entry_price),
                "contracts": pos.contracts,
                "entry_time": pos.entry_time.isoformat(),
            })

    return {"positions": opened, "count": len(opened)}
