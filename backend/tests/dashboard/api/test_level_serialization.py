from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from alpha_lab.dashboard.api.level_serialization import serialize_zone
from alpha_lab.dashboard.engine.models import KeyLevel, LevelSide, LevelType, LevelZone


def test_serialize_zone_preserves_touched_and_disabled_state() -> None:
    """Shared serializer keeps touched status and disabled-level metadata."""
    zone = LevelZone(
        zone_id="zone_1",
        representative_price=Decimal("21050.0"),
        side=LevelSide.HIGH,
        is_touched=True,
        levels=[
            KeyLevel(
                level_type=LevelType.PDH,
                price=Decimal("21050.0"),
                side=LevelSide.HIGH,
                available_from=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
                source_session_date=date(2026, 3, 2),
            ),
            KeyLevel(
                level_type=LevelType.MANUAL,
                price=Decimal("21050.0"),
                side=LevelSide.HIGH,
                available_from=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
                source_session_date=date(2026, 3, 2),
                is_manual=True,
            ),
        ],
    )

    payload = serialize_zone(zone, {LevelType.PDH})
    assert payload["zone_id"] == "zone_1"
    assert payload["is_touched"] is True
    assert payload["is_disabled"] is True
    assert payload["disabled_level_types"] == ["pdh"]
    assert payload["levels"][0]["type"] == "pdh"
    assert payload["levels"][1]["is_manual"] is True

