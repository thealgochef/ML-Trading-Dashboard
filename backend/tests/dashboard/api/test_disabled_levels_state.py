from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from alpha_lab.dashboard.api.server import DashboardState
from alpha_lab.dashboard.engine.level_engine import LevelEngine
from alpha_lab.dashboard.engine.models import (
    KeyLevel,
    LevelSide,
    LevelType,
    LevelZone,
)
from alpha_lab.dashboard.engine.touch_detector import TouchDetector
from alpha_lab.dashboard.pipeline.price_buffer import PriceBuffer
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate


def _trade(ts: datetime, price: float) -> TradeUpdate:
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=1,
        aggressor_side="BUY",
        symbol="NQH6",
    )


def test_dashboard_state_disabled_level_types_skip_pdh_zone() -> None:
    buffer = PriceBuffer()
    buffer.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.0))
    level_engine = LevelEngine(buffer)

    pdh_level = KeyLevel(
        level_type=LevelType.PDH,
        price=Decimal("20150.0"),
        side=LevelSide.HIGH,
        available_from=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
        source_session_date=date(2026, 3, 2),
    )
    pdh_zone = LevelZone(
        zone_id="pdh_zone",
        representative_price=Decimal("20150.0"),
        levels=[pdh_level],
        side=LevelSide.HIGH,
    )
    level_engine._zones = [pdh_zone]

    state = DashboardState(
        level_engine=level_engine,
        disabled_level_types={LevelType.PDH},
    )
    state.touch_detector = TouchDetector(
        level_engine,
        disabled_level_types=state.disabled_level_types,
    )

    event = state.touch_detector.on_trade(
        _trade(datetime(2026, 3, 2, 14, 35, tzinfo=UTC), 20150.0)
    )
    assert event is None
