"""
Config routes — TP/SL settings, signal mode, overlay toggles, and economic config.

GET  /api/config
PUT  /api/config
GET  /api/config/overlays
PUT  /api/config/overlays
PUT  /api/config/economic
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Request

from alpha_lab.dashboard.api.schemas import ConfigUpdateRequest, OverlayUpdateRequest

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("")
async def get_config(request: Request) -> dict:
    state = request.app.state.dashboard
    monitor = state.position_monitor
    return {
        "group_a_tp": float(monitor.get_group_tp("A")),
        "group_b_tp": float(monitor.get_group_tp("B")),
        "group_a_sl": float(monitor.get_group_sl("A")),
        "group_b_sl": float(monitor.get_group_sl("B")),
        "second_signal_mode": state.trade_executor.second_signal_mode,
    }


@router.put("")
async def update_config(body: ConfigUpdateRequest, request: Request) -> dict:
    state = request.app.state.dashboard
    monitor = state.position_monitor

    if body.group_a_tp is not None:
        monitor.set_group_tp("A", Decimal(str(body.group_a_tp)))
    if body.group_b_tp is not None:
        monitor.set_group_tp("B", Decimal(str(body.group_b_tp)))
    if body.group_a_sl is not None:
        monitor.set_group_sl("A", Decimal(str(body.group_a_sl)))
    if body.group_b_sl is not None:
        monitor.set_group_sl("B", Decimal(str(body.group_b_sl)))
    if body.second_signal_mode is not None:
        state.trade_executor.second_signal_mode = body.second_signal_mode

    return {
        "config": {
            "group_a_tp": float(monitor.get_group_tp("A")),
            "group_b_tp": float(monitor.get_group_tp("B")),
            "group_a_sl": float(monitor.get_group_sl("A")),
            "group_b_sl": float(monitor.get_group_sl("B")),
            "second_signal_mode": state.trade_executor.second_signal_mode,
        },
    }


@router.get("/overlays")
async def get_overlays(request: Request) -> dict:
    state = request.app.state.dashboard
    return {"overlays": dict(state.overlay_config)}


@router.put("/overlays")
async def update_overlays(body: OverlayUpdateRequest, request: Request) -> dict:
    state = request.app.state.dashboard
    for key, val in body.overlays.items():
        if key in state.overlay_config:
            state.overlay_config[key] = val
    return {"overlays": dict(state.overlay_config)}


@router.put("/economic")
async def update_economic_config(request: Request) -> dict:
    """Update economic analysis parameters and recompute metrics.

    Accepts any subset of EconomicConfig fields. Recomputes metrics
    from already-collected replay data without re-running the replay.
    """
    state = request.app.state.dashboard
    tracker = getattr(state, "economic_tracker", None)
    if tracker is None:
        return {"error": "no economic tracker (start a replay first)"}

    body = await request.json()
    tracker.config.update_from_dict(body)

    return {
        "config": tracker.config.to_dict(),
        "metrics": tracker.compute_tier1_metrics(),
    }
