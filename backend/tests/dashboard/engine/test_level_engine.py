"""
Phase 2 — Level Engine Tests

Tests key level computation from historical session data, manual level
management, and zone merging. The level engine determines WHERE the model
looks — incorrect levels mean the model evaluates the wrong price points.

Business context: Key levels (PDH, PDL, session highs/lows) are the
pre-computed trigger points where the CatBoost model evaluates order flow.
The trader also adds manual levels from their own analysis. All levels
within 3 NQ points merge into zones.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from alpha_lab.dashboard.engine.level_engine import LevelEngine
from alpha_lab.dashboard.engine.models import (
    LevelSide,
    LevelType,
)
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


def _populate_buffer_with_sessions(buf: PriceBuffer) -> None:
    """Populate buffer with trades spanning Asia, London, and NY RTH sessions.

    Trading date: 2026-03-02.
    All timestamps are in UTC.

    Session boundaries (ET → UTC, during EST = UTC-5):
      Asia:   18:00 ET prev day = 23:00 UTC 2026-03-01
              01:00 ET           = 06:00 UTC 2026-03-02
      London: 01:00 ET          = 06:00 UTC 2026-03-02
              08:00 ET           = 13:00 UTC 2026-03-02
      NY RTH: 09:30 ET          = 14:30 UTC 2026-03-02
              16:15 ET           = 21:15 UTC 2026-03-02
    """
    # Prior day NY RTH session (for PDH/PDL) — 2026-03-01 14:30-21:15 UTC
    prev_rth_start = datetime(2026, 3, 1, 14, 30, tzinfo=UTC)
    buf.add_trade(_trade(prev_rth_start, 20050.00))
    buf.add_trade(_trade(prev_rth_start + timedelta(hours=1), 20120.00))  # High
    buf.add_trade(_trade(prev_rth_start + timedelta(hours=2), 20000.00))  # Low
    buf.add_trade(_trade(prev_rth_start + timedelta(hours=3), 20080.00))

    # Asia session — 23:00 UTC 2026-03-01 to 06:00 UTC 2026-03-02
    asia_start = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
    buf.add_trade(_trade(asia_start, 20090.00))
    buf.add_trade(_trade(asia_start + timedelta(hours=1), 20110.00))  # Asia High
    buf.add_trade(_trade(asia_start + timedelta(hours=2), 20070.00))  # Asia Low
    buf.add_trade(_trade(asia_start + timedelta(hours=3), 20095.00))

    # London session — 06:00 UTC to 13:00 UTC 2026-03-02
    london_start = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)
    buf.add_trade(_trade(london_start, 20095.00))
    buf.add_trade(_trade(london_start + timedelta(hours=1), 20130.00))  # London High
    buf.add_trade(_trade(london_start + timedelta(hours=2), 20060.00))  # London Low
    buf.add_trade(_trade(london_start + timedelta(hours=3), 20100.00))


# ── Tests ────────────────────────────────────────────────────────


def test_pdh_pdl_computation():
    """PDH and PDL match the prior RTH session high/low."""
    buf = PriceBuffer()
    _populate_buffer_with_sessions(buf)
    engine = LevelEngine(buf)

    # Compute levels for 2026-03-02
    levels = engine.compute_levels(date(2026, 3, 2))

    pdh = [lv for lv in levels if lv.level_type == LevelType.PDH]
    pdl = [lv for lv in levels if lv.level_type == LevelType.PDL]

    assert len(pdh) == 1
    assert pdh[0].price == Decimal("20120.00")
    assert pdh[0].side == LevelSide.HIGH

    assert len(pdl) == 1
    assert pdl[0].price == Decimal("20000.00")
    assert pdl[0].side == LevelSide.LOW


def test_asia_session_high_low():
    """Asia levels computed from 18:00–01:00 ET window."""
    buf = PriceBuffer()
    _populate_buffer_with_sessions(buf)
    engine = LevelEngine(buf)

    levels = engine.compute_levels(date(2026, 3, 2))

    asia_high = [lv for lv in levels if lv.level_type == LevelType.ASIA_HIGH]
    asia_low = [lv for lv in levels if lv.level_type == LevelType.ASIA_LOW]

    assert len(asia_high) == 1
    assert asia_high[0].price == Decimal("20110.00")
    assert asia_high[0].side == LevelSide.HIGH

    assert len(asia_low) == 1
    assert asia_low[0].price == Decimal("20070.00")
    assert asia_low[0].side == LevelSide.LOW


def test_london_session_high_low():
    """London levels computed from 01:00–08:00 ET window."""
    buf = PriceBuffer()
    _populate_buffer_with_sessions(buf)
    engine = LevelEngine(buf)

    levels = engine.compute_levels(date(2026, 3, 2))

    london_high = [lv for lv in levels if lv.level_type == LevelType.LONDON_HIGH]
    london_low = [lv for lv in levels if lv.level_type == LevelType.LONDON_LOW]

    assert len(london_high) == 1
    assert london_high[0].price == Decimal("20130.00")
    assert london_high[0].side == LevelSide.HIGH

    assert len(london_low) == 1
    assert london_low[0].price == Decimal("20060.00")
    assert london_low[0].side == LevelSide.LOW


def test_level_available_from_timestamp():
    """PDH/PDL available before RTH open, Asia after 01:00, London after 08:00."""
    buf = PriceBuffer()
    _populate_buffer_with_sessions(buf)
    engine = LevelEngine(buf)

    levels = engine.compute_levels(date(2026, 3, 2))

    for level in levels:
        if level.level_type in (LevelType.PDH, LevelType.PDL):
            # PDH/PDL available at start of day (before RTH)
            assert level.available_from <= datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
        elif level.level_type in (LevelType.ASIA_HIGH, LevelType.ASIA_LOW):
            # Asia available after 01:00 ET = 06:00 UTC
            assert level.available_from == datetime(2026, 3, 2, 6, 0, tzinfo=UTC)
        elif level.level_type in (LevelType.LONDON_HIGH, LevelType.LONDON_LOW):
            # London available after 08:00 ET = 13:00 UTC
            assert level.available_from == datetime(2026, 3, 2, 13, 0, tzinfo=UTC)


def test_no_look_ahead():
    """Levels from incomplete sessions are never returned."""
    buf = PriceBuffer()

    # Only populate Asia — London not complete yet
    asia_start = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
    buf.add_trade(_trade(asia_start, 20090.00))
    buf.add_trade(_trade(asia_start + timedelta(hours=1), 20110.00))

    engine = LevelEngine(buf)

    # Compute at a time when London hasn't closed yet (e.g., 10:00 UTC = 05:00 ET)
    levels = engine.compute_levels(
        date(2026, 3, 2),
        current_time=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
    )

    # Asia should be present (completed), London should NOT
    asia = [lv for lv in levels if lv.level_type in (LevelType.ASIA_HIGH, LevelType.ASIA_LOW)]
    london = [lv for lv in levels if lv.level_type in (LevelType.LONDON_HIGH, LevelType.LONDON_LOW)]

    assert len(asia) == 2
    assert len(london) == 0


def test_on_session_close_adds_levels():
    """When London closes, london_high and london_low appear in active levels."""
    buf = PriceBuffer()
    _populate_buffer_with_sessions(buf)
    engine = LevelEngine(buf)

    # Initially compute with London not yet closed
    levels_before = engine.compute_levels(
        date(2026, 3, 2),
        current_time=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
    )
    london_before = [lv for lv in levels_before if lv.level_type == LevelType.LONDON_HIGH]
    assert len(london_before) == 0

    # After London closes (13:00 UTC = 08:00 ET)
    levels_after = engine.compute_levels(
        date(2026, 3, 2),
        current_time=datetime(2026, 3, 2, 13, 1, tzinfo=UTC),
    )
    london_after = [lv for lv in levels_after if lv.level_type == LevelType.LONDON_HIGH]
    assert len(london_after) == 1


def test_add_manual_level():
    """Manual level is added and appears in active zones."""
    buf = PriceBuffer()
    # Add a trade so we have a current price reference
    buf.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.00))
    engine = LevelEngine(buf)

    level = engine.add_manual_level(Decimal("20050.00"), date(2026, 3, 2))
    assert level.level_type == LevelType.MANUAL
    assert level.price == Decimal("20050.00")
    assert level.is_manual

    zones = engine.get_active_zones()
    zone_prices = [float(z.representative_price) for z in zones]
    assert 20050.0 in zone_prices


def test_remove_manual_level():
    """Removed manual level disappears from active zones."""
    buf = PriceBuffer()
    buf.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.00))
    engine = LevelEngine(buf)

    engine.add_manual_level(Decimal("20050.00"), date(2026, 3, 2))
    assert engine.remove_manual_level(Decimal("20050.00"))

    zones = engine.get_active_zones()
    zone_prices = [float(z.representative_price) for z in zones]
    assert 20050.0 not in zone_prices


def test_manual_level_side_detection():
    """Manual level above current price = HIGH side, below = LOW side."""
    buf = PriceBuffer()
    buf.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.00))
    engine = LevelEngine(buf)

    above = engine.add_manual_level(Decimal("20150.00"), date(2026, 3, 2))
    below = engine.add_manual_level(Decimal("20050.00"), date(2026, 3, 2))

    assert above.side == LevelSide.HIGH
    assert below.side == LevelSide.LOW


def test_zone_merging_within_3pts():
    """Two levels within 3.0 points merge into one zone."""
    buf = PriceBuffer()
    buf.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.00))
    engine = LevelEngine(buf)

    # Two manual levels 2.0 pts apart → should merge
    engine.add_manual_level(Decimal("20050.00"), date(2026, 3, 2))
    engine.add_manual_level(Decimal("20052.00"), date(2026, 3, 2))

    zones = engine.get_active_zones()
    # Should be 1 merged zone
    nearby_zones = [z for z in zones if 20049 < float(z.representative_price) < 20053]
    assert len(nearby_zones) == 1
    assert len(nearby_zones[0].levels) == 2


def test_zone_merging_chain():
    """Three levels chain-merge via single linkage."""
    buf = PriceBuffer()
    buf.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.00))
    engine = LevelEngine(buf)

    # A=20050, B=20052.5 (2.5 from A), C=20055 (2.5 from B, 5.0 from A)
    # Single linkage: A merges with B, B merges with C → all in one zone
    engine.add_manual_level(Decimal("20050.00"), date(2026, 3, 2))
    engine.add_manual_level(Decimal("20052.50"), date(2026, 3, 2))
    engine.add_manual_level(Decimal("20055.00"), date(2026, 3, 2))

    zones = engine.get_active_zones()
    nearby_zones = [z for z in zones if 20049 < float(z.representative_price) < 20056]
    assert len(nearby_zones) == 1
    assert len(nearby_zones[0].levels) == 3


def test_no_merge_beyond_3pts():
    """Two levels 3.5 points apart remain separate zones."""
    buf = PriceBuffer()
    buf.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.00))
    engine = LevelEngine(buf)

    engine.add_manual_level(Decimal("20050.00"), date(2026, 3, 2))
    engine.add_manual_level(Decimal("20053.50"), date(2026, 3, 2))

    zones = engine.get_active_zones()
    nearby_zones = [z for z in zones if 20049 < float(z.representative_price) < 20055]
    assert len(nearby_zones) == 2


def test_daily_reset():
    """reset_daily() clears manual levels and touch state."""
    buf = PriceBuffer()
    buf.add_trade(_trade(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 20100.00))
    engine = LevelEngine(buf)

    engine.add_manual_level(Decimal("20050.00"), date(2026, 3, 2))
    zones = engine.get_active_zones()
    assert len(zones) > 0

    # Touch a zone
    zone_id = zones[0].zone_id
    engine.mark_zone_touched(zone_id, datetime(2026, 3, 2, 15, 0, tzinfo=UTC))

    engine.reset_daily()

    # Manual levels cleared, touch state reset
    zones_after = engine.get_active_zones()
    manual_zones = [z for z in zones_after if any(lv.is_manual for lv in z.levels)]
    assert len(manual_zones) == 0


def test_level_persistence():
    """Levels are stored and retrievable after computation."""
    buf = PriceBuffer()
    _populate_buffer_with_sessions(buf)
    engine = LevelEngine(buf)

    levels = engine.compute_levels(date(2026, 3, 2))

    # Verify we get the right total: PDH + PDL + Asia H/L + London H/L = 6
    assert len(levels) == 6

    # Verify all levels are stored internally
    assert len(engine.all_levels) == 6
