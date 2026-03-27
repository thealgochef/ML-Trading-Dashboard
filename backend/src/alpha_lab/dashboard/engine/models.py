"""
Phase 2 data models for the observation engine.

Defines the enums and dataclasses used across level engine, touch detector,
observation manager, and feature computer. These are pure data definitions
with no business logic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class LevelType(Enum):
    PDH = "pdh"
    PDL = "pdl"
    ASIA_HIGH = "asia_high"
    ASIA_LOW = "asia_low"
    LONDON_HIGH = "london_high"
    LONDON_LOW = "london_low"
    MANUAL = "manual"


class LevelSide(Enum):
    HIGH = "high"   # PDH, asia_high, london_high — SHORT reversal
    LOW = "low"     # PDL, asia_low, london_low — LONG reversal


class TradeDirection(Enum):
    LONG = "long"
    SHORT = "short"


class ObservationStatus(Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    DISCARDED_FEED_DROP = "discarded_feed_drop"
    DISCARDED_TIME_CUTOFF = "discarded_time_cutoff"
    DISCARDED_LEVEL_DELETED = "discarded_level_deleted"


# Level type → side mapping
_HIGH_TYPES = {LevelType.PDH, LevelType.ASIA_HIGH, LevelType.LONDON_HIGH}
_LOW_TYPES = {LevelType.PDL, LevelType.ASIA_LOW, LevelType.LONDON_LOW}


def level_type_to_side(lt: LevelType) -> LevelSide:
    """Determine the side of a level type (HIGH or LOW)."""
    if lt in _HIGH_TYPES:
        return LevelSide.HIGH
    if lt in _LOW_TYPES:
        return LevelSide.LOW
    # Manual levels don't have a fixed side — determined by current price
    raise ValueError(f"Cannot determine side for {lt}; use current price context")


@dataclass
class KeyLevel:
    level_type: LevelType
    price: Decimal
    side: LevelSide
    available_from: datetime         # UTC — when this level becomes active
    source_session_date: date        # Which session produced this level
    is_manual: bool = False
    zone_id: str | None = None       # Set when merged with other levels


@dataclass
class LevelZone:
    zone_id: str
    representative_price: Decimal    # Average price of constituent levels
    levels: list[KeyLevel] = field(default_factory=list)
    side: LevelSide = LevelSide.HIGH
    is_touched: bool = False
    touched_at: datetime | None = None


@dataclass
class TouchEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)  # UTC
    level_zone: LevelZone = field(default_factory=lambda: LevelZone("", Decimal(0)))
    trade_direction: TradeDirection = TradeDirection.LONG
    price_at_touch: Decimal = Decimal(0)
    session: str = ""                # "asia", "london", "ny_rth", etc.


@dataclass
class ObservationWindow:
    event: TouchEvent = field(default_factory=TouchEvent)
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime = field(default_factory=datetime.now)
    status: ObservationStatus = ObservationStatus.ACTIVE
    trades_accumulated: list = field(default_factory=list)
    bbo_accumulated: list = field(default_factory=list)
    features: dict | None = None
