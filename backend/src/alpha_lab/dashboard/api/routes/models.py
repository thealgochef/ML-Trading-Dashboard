"""
Model routes — model upload, activation, rollback, and diagnostics.

GET  /api/models
GET  /api/models/diagnostic
POST /api/models/upload
POST /api/models/{version_id}/activate
POST /api/models/{version_id}/rollback
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/models", tags=["models"])


def _version_to_dict(version: dict) -> dict:
    return {
        "id": version["id"],
        "version": version["version"],
        "is_active": version["is_active"],
        "metrics": version.get("metrics"),
        "uploaded_at": version["uploaded_at"].isoformat() if version.get("uploaded_at") else None,
        "activated_at": (
            version["activated_at"].isoformat() if version.get("activated_at") else None
        ),
    }


@router.get("")
async def list_models(request: Request) -> dict:
    state = request.app.state.dashboard
    if state.model_manager is None:
        return {"active": None, "versions": []}

    active = state.model_manager.get_active_version()
    versions = state.model_manager.get_all_versions()
    return {
        "active": _version_to_dict(active) if active else None,
        "versions": [_version_to_dict(v) for v in versions],
    }


@router.post("/upload")
async def upload_model(request: Request, file: UploadFile) -> dict:
    state = request.app.state.dashboard
    if state.model_manager is None:
        return JSONResponse(
            status_code=503, content={"error": "Model manager not available"},
        )

    # Save uploaded file to temp location, then hand to model manager
    with tempfile.NamedTemporaryFile(suffix=".cbm", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = Path(tmp.name)

    version = state.model_manager.upload_model(tmp_path)
    tmp_path.unlink(missing_ok=True)

    return {"version": _version_to_dict(version)}


@router.post("/{version_id}/activate")
async def activate_model(version_id: int, request: Request) -> dict:
    state = request.app.state.dashboard
    if state.model_manager is None:
        return JSONResponse(
            status_code=503, content={"error": "Model manager not available"},
        )
    try:
        state.model_manager.activate_model(version_id)
    except ValueError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    return {"activated": True}


@router.post("/{version_id}/rollback")
async def rollback_model(version_id: int, request: Request) -> dict:
    state = request.app.state.dashboard
    if state.model_manager is None:
        return JSONResponse(
            status_code=503, content={"error": "Model manager not available"},
        )
    try:
        state.model_manager.rollback(version_id)
    except ValueError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    return {"activated": True}


@router.get("/diagnostic")
async def model_diagnostic(request: Request) -> dict:
    """Full system diagnostic — model, pipeline, predictions, trades, accounts."""
    state = request.app.state.dashboard

    # Model status
    mm = state.model_manager
    if mm is not None:
        active = mm.get_active_version()
        model_info = {
            "active_version": _version_to_dict(active) if active else None,
            "model_loaded": mm.model is not None,
            "total_versions": len(mm.get_all_versions()),
        }
    else:
        model_info = {
            "active_version": None,
            "model_loaded": False,
            "total_versions": 0,
        }

    # Pipeline / connection
    pipeline_info = {
        "connection_status": state.connection_status,
        "latest_price": state.latest_price,
        "latest_bid": state.latest_bid,
        "latest_ask": state.latest_ask,
        "session_ended": state.session_ended,
    }

    # Predictions
    preds = state.todays_predictions
    prediction_info = {
        "total_today": len(preds),
        "executable": sum(1 for p in preds if p.get("is_executable")),
        "resolved": sum(
            1 for p in preds
            if p.get("prediction_correct") is not None
        ),
        "correct": sum(1 for p in preds if p.get("prediction_correct")),
        "last_prediction": state.last_prediction,
    }

    # Trades
    trades = state.todays_trades
    by_reason: dict[str, int] = {}
    for t in trades:
        reason = t.get("exit_reason", "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1

    trade_info = {
        "total_today": len(trades),
        "total_pnl": sum(float(t.get("pnl", 0)) for t in trades),
        "by_reason": by_reason,
        "open_positions": sum(
            1 for a in state.account_manager.get_all_accounts()
            if a.has_position
        ),
    }

    # Outcome tracker
    ot = state.outcome_tracker
    outcome_info = {
        "active_trackers": ot.active_trackers if ot else 0,
    }

    # Accounts
    all_accts = state.account_manager.get_all_accounts()
    account_info = {
        "total": len(all_accts),
        "active": len(state.account_manager.get_active_accounts()),
        "tradeable": len(state.account_manager.get_tradeable_accounts()),
        "accounts": [
            {
                "id": a.account_id,
                "label": a.label,
                "group": a.group,
                "status": a.status.value,
                "balance": float(a.balance),
                "profit": float(a.profit),
                "tier": a.tier,
                "has_position": a.has_position,
                "daily_pnl": float(a.daily_pnl),
                "trade_count": sum(
                    1 for t in trades
                    if t.get("account_id") == a.account_id
                ),
            }
            for a in all_accts
        ],
    }

    return {
        "model": model_info,
        "pipeline": pipeline_info,
        "predictions": prediction_info,
        "trades": trade_info,
        "outcomes": outcome_info,
        "accounts": account_info,
    }
