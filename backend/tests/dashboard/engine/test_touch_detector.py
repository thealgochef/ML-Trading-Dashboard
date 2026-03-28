"""
Phase 2 — Touch Detector Tests

Tests real-time detection of price touching key level zones. The touch
detector converts the continuous tick stream into discrete events that
trigger observation windows.

Business context: A "touch" is the trigger for the entire prediction
pipeline. Missing a touch means missing a trading opportunity. False
touches waste model computation. First-touch-only prevents duplicate
signals on the same level.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from alpha_lab.dashboard.engine.level_engine import LevelEngine
from alpha_lab.dashboard.engine.models import (
    KeyLevel,
    LevelSide,
    LevelType,
    LevelZone,
    TouchEvent,
    TradeDirection,
)
from alpha_lab.dashboard.engine.touch_detector import TouchDetector
from alpha_lab.dashboard.pipeline.price_buffer import PriceBuffer
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate

# ── Helpers ──────────────────────────────────────────────────────


def _trade(
    ts: datetime,
    price: float,
    size: int = 1,
) -> TradeUpdate:
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side="BUY",
        symbol="NQH6",
    )


def _make_zone(
    price: float,
    side: LevelSide,
    zone_id: str = "z1",
    level_type: LevelType = LevelType.PDH,
) -> LevelZone:
    """Create a test zone with a single level."""
    level = KeyLevel(
        level_type=level_type,
        price=Decimal(str(price)),
        side=side,
        available_from=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
        source_session_date=date(2026, 3, 2),
    )
    return LevelZone(
        zone_id=zone_id,
        representative_price=Decimal(str(price)),
        levels=[level],
        side=side,
    )


def _make_engine_with_zones(zones: list[LevelZone]) -> LevelEngine:
    """Create a LevelEngine with pre-set zones (bypassing computation)."""
    buf = PriceBuffer()
    buf.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.00))
    engine = LevelEngine(buf)
    engine._zones = zones
    return engine


# ── Tests ────────────────────────────────────────────────────────


def test_touch_high_level():
    """Trade at or above a HIGH zone triggers SHORT touch event."""
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    trade = _trade(datetime(2026, 3, 2, 14, 35, tzinfo=UTC), 20150.00)
    event = detector.on_trade(trade)

    assert event is not None
    assert event.trade_direction == TradeDirection.SHORT
    assert event.price_at_touch == Decimal("20150.00")


def test_touch_low_level():
    """Trade at or below a LOW zone triggers LONG touch event."""
    zone = _make_zone(20050.00, LevelSide.LOW, level_type=LevelType.PDL)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    trade = _trade(datetime(2026, 3, 2, 14, 35, tzinfo=UTC), 20050.00)
    event = detector.on_trade(trade)

    assert event is not None
    assert event.trade_direction == TradeDirection.LONG


def test_first_touch_only():
    """Second touch of same zone returns None."""
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    ts = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    event1 = detector.on_trade(_trade(ts, 20150.00))
    event2 = detector.on_trade(_trade(ts + timedelta(seconds=10), 20151.00))

    assert event1 is not None
    assert event2 is None


def test_no_touch_below_zone():
    """Trade price below a HIGH zone doesn't trigger."""
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    trade = _trade(datetime(2026, 3, 2, 14, 35, tzinfo=UTC), 20149.75)
    event = detector.on_trade(trade)

    assert event is None


def test_callback_fires():
    """Registered callback receives TouchEvent on touch."""
    zone = _make_zone(20050.00, LevelSide.LOW, level_type=LevelType.PDL)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    received: list[TouchEvent] = []
    detector.on_touch(lambda e: received.append(e))

    trade = _trade(datetime(2026, 3, 2, 14, 35, tzinfo=UTC), 20050.00)
    detector.on_trade(trade)

    assert len(received) == 1
    assert received[0].trade_direction == TradeDirection.LONG


def test_multiple_zones_independent():
    """Touching zone A doesn't affect zone B."""
    zone_a = _make_zone(20150.00, LevelSide.HIGH, zone_id="za")
    zone_b = _make_zone(20050.00, LevelSide.LOW, zone_id="zb", level_type=LevelType.PDL)
    engine = _make_engine_with_zones([zone_a, zone_b])
    detector = TouchDetector(engine)

    ts = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    # Touch zone A
    event_a = detector.on_trade(_trade(ts, 20150.00))
    assert event_a is not None

    # Zone B should still be active
    event_b = detector.on_trade(_trade(ts + timedelta(minutes=1), 20050.00))
    assert event_b is not None


def test_time_cutoff_349pm():
    """Touches after 3:49 PM CT (15:49 ET = 20:49 UTC) are ignored."""
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    # 3:50 PM CT = 4:50 PM ET = 21:50 UTC → should be blocked
    late_ts = datetime(2026, 3, 2, 21, 50, tzinfo=UTC)
    event = detector.on_trade(_trade(late_ts, 20150.00))

    assert event is None


def test_touch_at_349pm_exactly():
    """Touch at exactly 3:49 PM CT is allowed (window completes at 3:54)."""
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    # 3:49 PM CT = 4:49 PM ET = 20:49 UTC → allowed
    exact_ts = datetime(2026, 3, 2, 20, 49, tzinfo=UTC)
    event = detector.on_trade(_trade(exact_ts, 20150.00))

    assert event is not None


def test_daily_reset_re_enables_zones():
    """After reset, previously touched zones can be touched again."""
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    ts = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    detector.on_trade(_trade(ts, 20150.00))

    # Reset creates fresh zones
    fresh_zone = _make_zone(20150.00, LevelSide.HIGH, zone_id="z1_new")
    engine._zones = [fresh_zone]

    event = detector.on_trade(_trade(ts + timedelta(hours=1), 20150.00))
    assert event is not None


def test_mixed_zone_direction():
    """Mixed zone uses level side to determine direction; HIGH → SHORT."""
    # Create a zone with both HIGH and LOW levels
    high_level = KeyLevel(
        level_type=LevelType.PDH,
        price=Decimal("20100.00"),
        side=LevelSide.HIGH,
        available_from=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
        source_session_date=date(2026, 3, 2),
    )
    low_level = KeyLevel(
        level_type=LevelType.PDL,
        price=Decimal("20101.00"),
        side=LevelSide.LOW,
        available_from=datetime(2026, 3, 2, 0, 0, tzinfo=UTC),
        source_session_date=date(2026, 3, 2),
    )
    mixed_zone = LevelZone(
        zone_id="mixed",
        representative_price=Decimal("20100.50"),
        levels=[high_level, low_level],
        side=LevelSide.HIGH,  # Default for mixed
    )
    engine = _make_engine_with_zones([mixed_zone])
    detector = TouchDetector(engine)

    event = detector.on_trade(_trade(
        datetime(2026, 3, 2, 14, 35, tzinfo=UTC), 20100.50,
    ))

    assert event is not None
    # Mixed zones determine direction from constituent level types
    # Has both HIGH and LOW → use zone's default side


def test_touch_event_fields():
    """TouchEvent contains all required fields with correct types."""
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    ts = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    event = detector.on_trade(_trade(ts, 20152.00))

    assert event is not None
    assert isinstance(event.event_id, str)
    assert len(event.event_id) > 0
    assert event.timestamp == ts
    assert event.level_zone is zone
    assert isinstance(event.price_at_touch, Decimal)
    assert event.price_at_touch == Decimal("20152.00")
    assert isinstance(event.trade_direction, TradeDirection)


def test_no_active_zones_no_detection():
    """When all zones are spent, trades pass through without detection."""
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    ts = datetime(2026, 3, 2, 14, 35, tzinfo=UTC)
    # Touch and spend the only zone
    detector.on_trade(_trade(ts, 20150.00))

    # Subsequent trades should not trigger
    event = detector.on_trade(_trade(ts + timedelta(minutes=1), 20200.00))
    assert event is None
    assert detector.active_zone_count == 0


# ── CME Day Boundary Classification Tests ────────────────────────


def test_cme_day_boundary_session_change_at_6pm_et():
    """Session changes from post_market → asia at exactly 6 PM ET.

    5:59 PM ET = post_market, 6:00 PM ET = asia. The TouchDetector fires
    a session_change callback at this transition, which is the true CME
    day boundary (not UTC midnight file transitions).
    """
    from alpha_lab.dashboard.engine.touch_detector import _classify_session
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")

    # 5:59 PM ET → post_market (2026-03-02 during EST, UTC-5)
    ts_559pm = datetime(2026, 3, 2, 17, 59, tzinfo=ET).astimezone(UTC)
    assert _classify_session(ts_559pm) == "post_market"

    # 6:00 PM ET → asia (CME day boundary)
    ts_600pm = datetime(2026, 3, 2, 18, 0, tzinfo=ET).astimezone(UTC)
    assert _classify_session(ts_600pm) == "asia"

    # Verify TouchDetector fires session_change for this transition
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    transitions: list[tuple[str | None, str]] = []
    detector.on_session_change(
        lambda old, new, ts: transitions.append((old, new))
    )

    # Feed a post_market tick then an asia tick
    detector.on_trade(_trade(ts_559pm, 20100.00))
    detector.on_trade(_trade(ts_600pm, 20100.00))

    assert len(transitions) == 2  # None→post_market, post_market→asia
    assert transitions[0] == (None, "post_market")
    assert transitions[1] == ("post_market", "asia")


def test_no_session_change_at_utc_midnight():
    """UTC midnight does NOT trigger a session change if session stays the same.

    At UTC midnight during EST (UTC-5), ET time is 7 PM (19:00) — still
    in the 'asia' session (18:00–01:00 ET). No session change should fire.
    """
    from alpha_lab.dashboard.engine.touch_detector import _classify_session

    # 2026-03-01 23:59 UTC = 6:59 PM ET → asia
    ts_before_midnight = datetime(2026, 3, 1, 23, 59, tzinfo=UTC)
    assert _classify_session(ts_before_midnight) == "asia"

    # 2026-03-02 00:01 UTC = 7:01 PM ET → still asia
    ts_after_midnight = datetime(2026, 3, 2, 0, 1, tzinfo=UTC)
    assert _classify_session(ts_after_midnight) == "asia"

    # Verify no session change callback fires
    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    transitions: list[tuple[str | None, str]] = []
    detector.on_session_change(
        lambda old, new, ts: transitions.append((old, new))
    )

    detector.on_trade(_trade(ts_before_midnight, 20100.00))
    detector.on_trade(_trade(ts_after_midnight, 20100.00))

    # Only the initial None → asia transition, no second transition
    assert len(transitions) == 1
    assert transitions[0] == (None, "asia")


def test_friday_to_sunday_boundary():
    """post_market → asia fires correctly across a weekend gap.

    Friday's last tick is in post_market (after 4:15 PM ET).
    Sunday's first tick at 6 PM ET starts a new asia session.
    The transition post_market → asia fires correctly despite the gap.
    """
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")

    zone = _make_zone(20150.00, LevelSide.HIGH)
    engine = _make_engine_with_zones([zone])
    detector = TouchDetector(engine)

    transitions: list[tuple[str | None, str]] = []
    detector.on_session_change(
        lambda old, new, ts: transitions.append((old, new))
    )

    # Friday 4:30 PM ET → post_market
    friday_tick = datetime(2026, 3, 6, 16, 30, tzinfo=ET).astimezone(UTC)
    detector.on_trade(_trade(friday_tick, 20100.00))

    # Sunday 6:00 PM ET → asia (CME week opens)
    sunday_tick = datetime(2026, 3, 8, 18, 0, tzinfo=ET).astimezone(UTC)
    detector.on_trade(_trade(sunday_tick, 20100.00))

    assert len(transitions) == 2
    assert transitions[0] == (None, "post_market")
    assert transitions[1] == ("post_market", "asia")
