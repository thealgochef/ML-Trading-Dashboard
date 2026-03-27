"""
Key level computation and management.

Computes session highs/lows (PDH, PDL, Asia, London) from the price buffer's
trade history. Manages manual levels from the trader. Merges levels within
3 NQ points into zones using single-linkage clustering.

Session boundaries (Eastern Time):
    Asia:   18:00 – 01:00  (crosses midnight)
    London: 01:00 – 08:00
    NY RTH: 09:30 – 16:15
    Day boundary: 18:00 ET (6 PM)
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from alpha_lab.dashboard.engine.models import (
    KeyLevel,
    LevelSide,
    LevelType,
    LevelZone,
)
from alpha_lab.dashboard.pipeline.price_buffer import PriceBuffer

ET = ZoneInfo("America/New_York")

# Session boundaries in ET
_ASIA_START = time(18, 0)   # Previous calendar day
_ASIA_END = time(1, 0)      # Current calendar day
_LONDON_START = time(1, 0)
_LONDON_END = time(8, 0)
_NY_RTH_START = time(9, 30)
_NY_RTH_END = time(16, 15)

# Zone merging threshold (NQ points)
PROXIMITY_THRESHOLD = Decimal("3.0")


class LevelEngine:
    """Computes key levels from prior sessions and manages manual levels.

    Levels are computed from completed prior sessions only — no look-ahead.
    Manual levels are accepted from the trader and treated identically to
    auto-detected levels. Levels within 3 points are merged into zones.
    """

    def __init__(self, price_buffer: PriceBuffer) -> None:
        self._buffer = price_buffer
        self._levels: list[KeyLevel] = []
        self._manual_levels: list[KeyLevel] = []
        self._zones: list[LevelZone] = []

    # ── Public interface ──────────────────────────────────────────

    def compute_levels(
        self,
        trading_date: date,
        current_time: datetime | None = None,
    ) -> list[KeyLevel]:
        """Compute all auto-detected levels for the given trading day.

        Args:
            trading_date: The trading date to compute levels for.
            current_time: If provided, only return levels from sessions
                that have completed by this time (no look-ahead).
                If None, returns all levels (assumes all sessions complete).
        """
        self._levels = []

        # PDH/PDL from prior day's NY RTH
        self._compute_pdh_pdl(trading_date)

        # Asia session levels
        self._compute_session_levels(
            trading_date, "asia",
            LevelType.ASIA_HIGH, LevelType.ASIA_LOW,
            current_time,
        )

        # London session levels
        self._compute_session_levels(
            trading_date, "london",
            LevelType.LONDON_HIGH, LevelType.LONDON_LOW,
            current_time,
        )

        self._rebuild_zones()
        return list(self._levels)

    def add_manual_level(
        self,
        price: Decimal,
        trading_date: date,
    ) -> KeyLevel:
        """Add a manual level. Side is determined by current price."""
        current_price = self._buffer.latest_price
        if current_price is not None and price >= current_price:
            side = LevelSide.HIGH
        else:
            side = LevelSide.LOW

        level = KeyLevel(
            level_type=LevelType.MANUAL,
            price=price,
            side=side,
            available_from=datetime.now(UTC),
            source_session_date=trading_date,
            is_manual=True,
        )
        self._manual_levels.append(level)
        self._rebuild_zones()
        return level

    def remove_manual_level(self, price: Decimal) -> bool:
        """Remove a manual level. Returns True if found and removed."""
        for i, level in enumerate(self._manual_levels):
            if level.price == price:
                self._manual_levels.pop(i)
                self._rebuild_zones()
                return True
        return False

    def get_active_zones(self) -> list[LevelZone]:
        """Return all active (untouched) level zones."""
        return [z for z in self._zones if not z.is_touched]

    @property
    def all_zones(self) -> list[LevelZone]:
        """Return all zones including touched ones."""
        return list(self._zones)

    def mark_zone_touched(
        self,
        zone_id: str,
        touched_at: datetime,
    ) -> None:
        """Mark a zone as touched. It will no longer be active."""
        for zone in self._zones:
            if zone.zone_id == zone_id:
                zone.is_touched = True
                zone.touched_at = touched_at
                break

    def reset_daily(self) -> None:
        """Clear manual levels and reset touch state. Called at 6 PM ET."""
        self._manual_levels.clear()
        self._levels.clear()
        self._zones.clear()

    @property
    def all_levels(self) -> list[KeyLevel]:
        return list(self._levels) + list(self._manual_levels)

    # ── Internal ──────────────────────────────────────────────────

    def _compute_pdh_pdl(self, trading_date: date) -> None:
        """Compute PDH/PDL from the prior day's NY RTH session.

        RTH runs 9:30 AM – 4:15 PM ET. This matches how the CatBoost
        model was trained and how run_backtest.py computes PDH/PDL.
        """
        prev_day = trading_date - timedelta(days=1)

        # Previous day RTH: 9:30 AM ET → 4:15 PM ET
        session_start_et = datetime.combine(prev_day, _NY_RTH_START, tzinfo=ET)
        session_end_et = datetime.combine(prev_day, _NY_RTH_END, tzinfo=ET)
        rth_start_utc = session_start_et.astimezone(UTC)
        rth_end_utc = session_end_et.astimezone(UTC)

        result = self._buffer.get_high_low_in_range(rth_start_utc, rth_end_utc)
        if result is None:
            return

        high, low = result

        # PDH/PDL available from start of the new day
        avail = datetime.combine(trading_date, time(0, 0), tzinfo=ET).astimezone(UTC)

        self._levels.append(KeyLevel(
            level_type=LevelType.PDH,
            price=high,
            side=LevelSide.HIGH,
            available_from=avail,
            source_session_date=prev_day,
        ))
        self._levels.append(KeyLevel(
            level_type=LevelType.PDL,
            price=low,
            side=LevelSide.LOW,
            available_from=avail,
            source_session_date=prev_day,
        ))

    def _compute_session_levels(
        self,
        trading_date: date,
        session_name: str,
        high_type: LevelType,
        low_type: LevelType,
        current_time: datetime | None,
    ) -> None:
        """Compute high/low for a given session."""
        start_utc, end_utc = self._session_boundaries_utc(trading_date, session_name)

        # No look-ahead: only return if session is complete
        if current_time is not None and current_time < end_utc:
            return

        result = self._buffer.get_high_low_in_range(start_utc, end_utc)
        if result is None:
            return

        high, low = result

        self._levels.append(KeyLevel(
            level_type=high_type,
            price=high,
            side=LevelSide.HIGH,
            available_from=end_utc,
            source_session_date=trading_date,
        ))
        self._levels.append(KeyLevel(
            level_type=low_type,
            price=low,
            side=LevelSide.LOW,
            available_from=end_utc,
            source_session_date=trading_date,
        ))

    def _session_boundaries_utc(
        self,
        trading_date: date,
        session_name: str,
    ) -> tuple[datetime, datetime]:
        """Return (start_utc, end_utc) for a session on the given trading date."""
        if session_name == "asia":
            # Asia: 18:00 ET prev day to 01:00 ET current day
            prev_day = trading_date - timedelta(days=1)
            start_et = datetime.combine(prev_day, _ASIA_START, tzinfo=ET)
            end_et = datetime.combine(trading_date, _ASIA_END, tzinfo=ET)
        elif session_name == "london":
            start_et = datetime.combine(trading_date, _LONDON_START, tzinfo=ET)
            end_et = datetime.combine(trading_date, _LONDON_END, tzinfo=ET)
        elif session_name == "ny_rth":
            start_et = datetime.combine(trading_date, _NY_RTH_START, tzinfo=ET)
            end_et = datetime.combine(trading_date, _NY_RTH_END, tzinfo=ET)
        else:
            raise ValueError(f"Unknown session: {session_name}")

        return start_et.astimezone(UTC), end_et.astimezone(UTC)

    def _rebuild_zones(self) -> None:
        """Rebuild zones from all levels using single-linkage clustering."""
        all_levels = list(self._levels) + list(self._manual_levels)
        if not all_levels:
            self._zones = []
            return

        # Preserve existing touch state
        old_touch_state: dict[str, tuple[bool, datetime | None]] = {}
        for z in self._zones:
            old_touch_state[z.zone_id] = (z.is_touched, z.touched_at)

        # Sort by price
        sorted_levels = sorted(all_levels, key=lambda lv: lv.price)

        # Single-linkage clustering
        groups: list[list[KeyLevel]] = [[sorted_levels[0]]]
        for level in sorted_levels[1:]:
            if level.price - groups[-1][-1].price <= PROXIMITY_THRESHOLD:
                groups[-1].append(level)
            else:
                groups.append([level])

        # Build zones
        self._zones = []
        for group in groups:
            rep_price = sum(lv.price for lv in group) / len(group)

            # Determine zone side from constituent levels
            sides = {lv.side for lv in group}
            zone_side = sides.pop() if len(sides) == 1 else LevelSide.HIGH

            zone_id = str(uuid.uuid4())[:8]

            # Assign zone_id to constituent levels
            for level in group:
                level.zone_id = zone_id

            zone = LevelZone(
                zone_id=zone_id,
                representative_price=rep_price,
                levels=group,
                side=zone_side,
            )

            self._zones.append(zone)
