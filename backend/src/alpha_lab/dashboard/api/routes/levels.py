"""
Level routes — manual level management.

GET    /api/levels
POST   /api/levels/manual
DELETE /api/levels/manual/{price}
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from alpha_lab.dashboard.api.schemas import AddManualLevelRequest

router = APIRouter(prefix="/api/levels", tags=["levels"])


@router.get("")
async def get_levels(request: Request) -> dict:
    state = request.app.state.dashboard
    if state.level_engine is None:
        return {"zones": [], "manual_levels": []}

    zones = []
    for zone in state.level_engine.all_zones:
        zones.append({
            "zone_id": zone.zone_id,
            "price": float(zone.representative_price),
            "side": zone.side.value,
            "is_touched": zone.is_touched,
            "levels": [
                {
                    "type": lv.level_type.value,
                    "price": float(lv.price),
                    "is_manual": lv.is_manual,
                }
                for lv in zone.levels
            ],
        })

    manual = [
        {"price": float(lv.price), "type": lv.level_type.value}
        for lv in state.level_engine.all_levels
        if lv.is_manual
    ]

    return {"zones": zones, "manual_levels": manual}


@router.post("/manual")
async def add_manual_level(body: AddManualLevelRequest, request: Request) -> dict:
    state = request.app.state.dashboard
    if state.level_engine is None:
        return JSONResponse(
            status_code=503, content={"error": "Level engine not available"},
        )
    today = datetime.now(UTC).date() if not isinstance(date.today(), date) else date.today()
    level = state.level_engine.add_manual_level(Decimal(str(body.price)), today)
    return {
        "level": {
            "type": level.level_type.value,
            "price": float(level.price),
            "side": level.side.value,
            "is_manual": level.is_manual,
        },
    }


@router.delete("/manual/{price}")
async def delete_manual_level(price: float, request: Request) -> dict:
    state = request.app.state.dashboard
    if state.level_engine is None:
        return JSONResponse(
            status_code=503, content={"error": "Level engine not available"},
        )
    removed = state.level_engine.remove_manual_level(Decimal(str(price)))
    if not removed:
        return JSONResponse(
            status_code=404, content={"error": f"Manual level at {price} not found"},
        )
    return {"deleted": True}
