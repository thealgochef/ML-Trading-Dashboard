"""
Touch detector — monitors live tick stream against active level zones.

Detects first-touch events when price reaches or surpasses a level zone.
Uses RTH-only spending: non-RTH touches (London/Asia/pre-market) fire
events but do NOT mark the zone as spent for RTH. Each non-RTH session
enforces its own first-touch rule (no repeated touches within the same
session). When RTH starts, all zones are available regardless of
overnight touches.

Respects the 3:49 PM CT time cutoff to ensure 5-minute observation
windows complete before the 3:55 PM CT hard flatten.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

from alpha_lab.dashboard.engine.level_engine import LevelEngine
from alpha_lab.dashboard.engine.models import (
    LevelSide,
    TouchEvent,
    TradeDirection,
)
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")

# Last new observation: 3:49 PM ET (15:49 ET)
# This ensures the 5-minute window completes by 3:54 PM ET,
# one minute before the 3:55 PM ET hard flatten.
# NOTE: Must use ET to match flatten time (position_monitor.py).
_CUTOFF_HOUR = 15
_CUTOFF_MINUTE = 49


def _classify_session(ts_utc: datetime) -> str:
    """Classify a UTC timestamp into a session name."""
    from datetime import time
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    ts_et = ts_utc.astimezone(et)
    t = ts_et.time()

    if t >= time(18, 0) or t < time(1, 0):
        return "asia"
    if time(1, 0) <= t < time(8, 0):
        return "london"
    if time(8, 0) <= t < time(9, 30):
        return "pre_market"
    if time(9, 30) <= t < time(16, 15):
        return "ny_rth"
    return "post_market"


class TouchDetector:
    """Monitors live trades against active level zones.

    Uses RTH-only spending: non-RTH touches fire events but don't mark
    zones as spent for RTH. Each non-RTH session tracks its own
    first-touch state independently.
    """

    def __init__(self, level_engine: LevelEngine) -> None:
        self._engine = level_engine
        self._callbacks: list[Callable[[TouchEvent], None]] = []
        self._session_change_callbacks: list[Callable[[str | None, str, datetime], None]] = []
        # Session-local touch tracking for non-RTH sessions.
        # Prevents repeated touches within the same non-RTH session
        # without permanently spending the zone for RTH.
        self._session_touches: set[str] = set()
        self._current_session: str | None = None

    def on_trade(self, trade: TradeUpdate) -> TouchEvent | None:
        """Process a trade. Returns a TouchEvent if this trade triggers
        a level touch, otherwise None."""
        session = _classify_session(trade.timestamp)

        # Detect session changes BEFORE the time cutoff — session
        # transitions (especially post_market → asia at 6 PM ET for
        # CME day boundary) must always fire regardless of whether
        # touch detection is active.
        if session != self._current_session:
            old_session = self._current_session
            logger.info(
                "Session changed: %s -> %s, active_zones=%d",
                old_session, session,
                len(self._engine.get_active_zones()),
            )
            self._session_touches.clear()
            self._current_session = session

            # Notify listeners (e.g. level recomputation, day reset)
            for cb in self._session_change_callbacks:
                try:
                    cb(old_session, session, trade.timestamp)
                except Exception:
                    logger.exception("Session change callback failed")

        # Time cutoff: no new touches after 3:49 PM ET
        ts_et = trade.timestamp.astimezone(ET)
        if (ts_et.hour > _CUTOFF_HOUR or
                (ts_et.hour == _CUTOFF_HOUR and ts_et.minute > _CUTOFF_MINUTE)):
            return None

        for zone in self._engine.get_active_zones():
            # During non-RTH: skip if already touched in this session
            if session != "ny_rth" and zone.zone_id in self._session_touches:
                continue

            touched = False

            if zone.side == LevelSide.HIGH:
                # HIGH zone: trade at or above -> SHORT
                if trade.price >= zone.representative_price:
                    touched = True
                    direction = TradeDirection.SHORT
            elif zone.side == LevelSide.LOW:
                # LOW zone: trade at or below -> LONG
                if trade.price <= zone.representative_price:
                    touched = True
                    direction = TradeDirection.LONG
            else:
                continue

            if touched:
                level_types = ", ".join(
                    lv.level_type.value for lv in zone.levels
                )
                logger.info(
                    "Level touch: %s @ %.2f, direction=%s, session=%s, zone=%s",
                    level_types, float(zone.representative_price),
                    direction.value, session, zone.zone_id[:8],
                )

                if session == "ny_rth":
                    # RTH touch: permanently spend the zone
                    self._engine.mark_zone_touched(zone.zone_id, trade.timestamp)
                else:
                    # Non-RTH touch: session-local only, zone stays
                    # available for RTH
                    self._session_touches.add(zone.zone_id)

                event = TouchEvent(
                    event_id=str(uuid.uuid4()),
                    timestamp=trade.timestamp,
                    level_zone=zone,
                    trade_direction=direction,
                    price_at_touch=trade.price,
                    session=session,
                )

                for cb in self._callbacks:
                    cb(event)

                return event

        return None

    def on_touch(self, callback: Callable[[TouchEvent], None]) -> None:
        """Register callback for touch events."""
        self._callbacks.append(callback)

    def on_session_change(self, callback: Callable[[str | None, str, datetime], None]) -> None:
        """Register callback for session transitions.

        Callback receives (old_session, new_session, timestamp).
        Fired when the tick stream crosses a session boundary.
        """
        self._session_change_callbacks.append(callback)

    @property
    def active_zone_count(self) -> int:
        return len(self._engine.get_active_zones())
