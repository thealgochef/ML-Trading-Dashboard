"""Shared level-zone serialization helpers for API + WebSocket payloads."""

from __future__ import annotations

from collections.abc import Iterable

from alpha_lab.dashboard.engine.models import LevelType, LevelZone


def serialize_zone(
    zone: LevelZone,
    disabled_level_types: set[LevelType] | None = None,
) -> dict:
    """Serialize a single LevelZone for frontend payloads."""
    disabled = disabled_level_types or set()
    zone_disabled_types = sorted({
        lv.level_type.value
        for lv in zone.levels
        if lv.level_type in disabled
    })

    return {
        "zone_id": zone.zone_id,
        "price": float(zone.representative_price),
        "side": zone.side.value,
        "is_touched": zone.is_touched,
        "is_disabled": len(zone_disabled_types) > 0,
        "disabled_level_types": zone_disabled_types,
        "levels": [
            {
                "type": lv.level_type.value,
                "price": float(lv.price),
                "is_manual": lv.is_manual,
            }
            for lv in zone.levels
        ],
    }


def serialize_zones(
    zones: Iterable[LevelZone],
    disabled_level_types: set[LevelType] | None = None,
) -> list[dict]:
    """Serialize a collection of zones with consistent semantics."""
    return [serialize_zone(z, disabled_level_types) for z in zones]

