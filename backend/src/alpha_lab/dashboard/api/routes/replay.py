"""
Replay routes — start, control, and monitor replay pipelines.

POST /api/replay/start      — Create and start a replay pipeline
POST /api/replay/control    — Play/pause/step/set_speed
GET  /api/replay/status     — Current replay state
GET  /api/replay/economics  — Tier 1 economic metrics
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/replay", tags=["replay"])


class ReplayStartRequest(BaseModel):
    start_date: str
    end_date: str
    speed: float = 1.0


class ReplayControlRequest(BaseModel):
    action: str  # "play" | "pause" | "step" | "set_speed" | "set_step_mode"
    speed: float | None = None
    enabled: bool | None = None


@router.post("/start")
async def replay_start(request: Request, body: ReplayStartRequest) -> dict:
    """Create and start a replay pipeline.

    Stops any existing pipeline, resets all state, then creates a fresh
    replay pipeline with the given date range and speed.
    """
    state = request.app.state.dashboard

    if not getattr(state, "replay_mode", False):
        return {"error": "server not in replay mode (start with --replay)"}

    from alpha_lab.dashboard.api.server import (
        _reset_state_for_replay,
        start_replay_pipeline,
    )
    from alpha_lab.dashboard.config.settings import DashboardSettings

    # Stop existing pipeline if running
    if state.pipeline is not None and state.pipeline.is_running:
        await state.pipeline.stop()
        logger.info("Stopped previous replay pipeline")

    # Reset all state
    _reset_state_for_replay(state)

    # Start new pipeline
    settings = DashboardSettings()
    await start_replay_pipeline(
        state,
        data_dir=settings.replay_data_dir,
        start_date=body.start_date,
        end_date=body.end_date,
        speed=body.speed,
    )

    return {"ok": True, "status": "started_paused"}


@router.post("/control")
async def replay_control(request: Request, body: ReplayControlRequest) -> dict:
    """Control a running replay pipeline."""
    state = request.app.state.dashboard

    if not getattr(state, "replay_mode", False):
        return {"error": "not in replay mode"}

    from alpha_lab.dashboard.pipeline.replay_client import ReplayClient

    client = getattr(state.pipeline, "_client", None)
    if not isinstance(client, ReplayClient):
        return {"error": "no replay client (call /api/replay/start first)"}

    action = body.action

    if action == "play":
        client.play()
    elif action == "pause":
        client.pause()
    elif action == "step":
        client.step()
    elif action == "set_speed":
        client.set_speed(float(body.speed or 1.0))
    elif action == "set_step_mode":
        client.set_step_mode(bool(body.enabled))
    else:
        return {"error": f"unknown action: {action}"}

    return {
        "ok": True,
        "action": action,
        "paused": not client._pause_event.is_set(),
        "step_mode": client._step_mode,
        "speed": client._speed,
        "replay_complete": client.replay_complete,
        "current_date": client.current_date,
    }


@router.get("/status")
async def replay_status(request: Request) -> dict:
    """Return current replay pipeline state."""
    state = request.app.state.dashboard

    if not getattr(state, "replay_mode", False):
        return {"status": "not_replay_mode"}

    from alpha_lab.dashboard.pipeline.replay_client import ReplayClient

    client = getattr(state.pipeline, "_client", None) if state.pipeline else None
    if not isinstance(client, ReplayClient):
        return {
            "status": "idle",
            "replay_mode": True,
            "pipeline_running": False,
        }

    return {
        "status": "running" if not client.replay_complete else "complete",
        "replay_mode": True,
        "pipeline_running": state.pipeline.is_running if state.pipeline else False,
        "current_date": client.current_date,
        "current_timestamp": (
            client.current_timestamp.isoformat()
            if client.current_timestamp
            else None
        ),
        "replay_complete": client.replay_complete,
        "paused": not client._pause_event.is_set(),
        "speed": client._speed,
        "step_mode": client._step_mode,
        "preloading": client._preloading,
        "tick_count": state.tick_count,
        "prediction_count": state.prediction_count,
        "trade_count": len(state.todays_trades),
    }


@router.get("/economics")
async def replay_economics(request: Request) -> dict:
    """Return Tier 1 economic metrics + current config.

    Computes metrics on-demand from data collected during replay.
    """
    state = request.app.state.dashboard

    if not getattr(state, "replay_mode", False):
        return {"error": "not in replay mode"}

    tracker = getattr(state, "economic_tracker", None)
    if tracker is None:
        return {"error": "no economic tracker (start a replay first)"}

    return {
        "config": tracker.config.to_dict(),
        "metrics": tracker.compute_tier1_metrics(),
    }
