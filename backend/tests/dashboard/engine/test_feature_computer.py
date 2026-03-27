"""
Phase 2 — Feature Computer Tests

Tests the computation of the 3 validated CatBoost features from
observation window tick data. Feature parity with the batch experiment
is critical — divergence means the model receives inputs it wasn't
trained on.

Business context: The experiment proved that ONLY these 3 features
(time beyond level, time within 2pts, absorption ratio) have predictive
power. Reversal precision is 86.1% when computed correctly. Wrong
feature values = wrong predictions = lost money.

Computation follows the batch code in src/alpha_lab/experiment/features.py:
- Tempo features use mid-price from BBO events, duration across ALL events
- Absorption uses trade prices/volumes, ±0.50 pts proximity, at/(at+through)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_lab.dashboard.engine.feature_computer import FeatureComputer
from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate

# ── Helpers ──────────────────────────────────────────────────────

LEVEL_PRICE = Decimal("20100.00")
WINDOW_START = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(minutes=5)


def _trade(
    ts_offset_s: float = 0,
    price: float = 20100.00,
    size: int = 5,
    side: str = "BUY",
) -> TradeUpdate:
    ts = WINDOW_START + timedelta(seconds=ts_offset_s)
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side=side,
        symbol="NQH6",
    )


def _bbo(
    ts_offset_s: float = 0,
    bid: float = 20099.75,
    ask: float = 20100.25,
) -> BBOUpdate:
    ts = WINDOW_START + timedelta(seconds=ts_offset_s)
    return BBOUpdate(
        timestamp=ts,
        bid_price=Decimal(str(bid)),
        bid_size=15,
        ask_price=Decimal(str(ask)),
        ask_size=12,
        symbol="NQH6",
    )


# ── Tests ────────────────────────────────────────────────────────


def test_time_beyond_level_long():
    """LONG event: correctly sums time where mid-price < level."""
    fc = FeatureComputer()

    # BBO with mid = 20099.875 (below level 20100.00) → beyond for LONG
    bbo_updates = [
        _bbo(ts_offset_s=0, bid=20099.50, ask=20100.25),   # mid = 20099.875
    ]
    # BBO shifts mid above level at t=60s
    bbo_updates.append(
        _bbo(ts_offset_s=60, bid=20100.00, ask=20100.50),  # mid = 20100.25
    )
    # Stays above for the rest

    trades = [_trade(ts_offset_s=30, price=20099.75)]  # trade in between

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    # Events sorted by time: bbo@0s(mid=20099.875), trade@30s, bbo@60s(mid=20100.25)
    # t=0 to t=30: mid=20099.875 < 20100 → 30s beyond
    # t=30 to t=60: mid=20099.875 (still, last BBO) < 20100 → 30s beyond
    # t=60 to window_end(300s): mid=20100.25 >= 20100 → 0s beyond
    # Total: 60s beyond
    assert abs(result["int_time_beyond_level"] - 60.0) < 0.01


def test_time_beyond_level_short():
    """SHORT event: correctly sums time where mid-price > level."""
    fc = FeatureComputer()

    # BBO with mid = 20100.125 (above level 20100.00) → beyond for SHORT
    bbo_updates = [
        _bbo(ts_offset_s=0, bid=20100.00, ask=20100.25),   # mid = 20100.125
    ]
    # BBO shifts mid below level at t=120s
    bbo_updates.append(
        _bbo(ts_offset_s=120, bid=20099.50, ask=20099.75),  # mid = 20099.625
    )

    trades = [_trade(ts_offset_s=60, price=20100.25)]

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.SHORT,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    # Events: bbo@0(mid=20100.125), trade@60, bbo@120(mid=20099.625)
    # t=0 to t=60: mid=20100.125 > 20100 → 60s beyond
    # t=60 to t=120: mid=20100.125 (still) > 20100 → 60s beyond
    # t=120 to 300: mid=20099.625 <= 20100 → 0s
    # Total: 120s
    assert abs(result["int_time_beyond_level"] - 120.0) < 0.01


def test_time_within_2pts():
    """Correctly sums time where |mid - level| <= 2.0 points."""
    fc = FeatureComputer()

    # mid = 20100.125 → |20100.125 - 20100| = 0.125 ≤ 2.0 → within
    bbo_updates = [
        _bbo(ts_offset_s=0, bid=20100.00, ask=20100.25),   # mid = 20100.125
    ]
    # mid = 20103.00 → |20103 - 20100| = 3.0 > 2.0 → NOT within
    bbo_updates.append(
        _bbo(ts_offset_s=100, bid=20102.75, ask=20103.25),  # mid = 20103.0
    )
    # mid = 20099.00 → |20099 - 20100| = 1.0 ≤ 2.0 → within
    bbo_updates.append(
        _bbo(ts_offset_s=200, bid=20098.75, ask=20099.25),  # mid = 20099.0
    )

    trades = []

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    # Events: bbo@0(mid=20100.125), bbo@100(mid=20103), bbo@200(mid=20099)
    # t=0 to t=100: mid=20100.125 within → 100s
    # t=100 to t=200: mid=20103 NOT within → 0s
    # t=200 to 300: mid=20099 within → 100s
    # Total: 200s
    assert abs(result["int_time_within_2pts"] - 200.0) < 0.01


def test_absorption_ratio_high_absorption():
    """Heavy volume at level / light volume through = high ratio."""
    fc = FeatureComputer()

    bbo_updates = [_bbo(ts_offset_s=0)]

    # All trades at the level (within ±0.50 pts)
    trades = [
        _trade(ts_offset_s=10, price=20100.00, size=50),   # at level
        _trade(ts_offset_s=20, price=20100.25, size=30),   # at level (+0.25)
        _trade(ts_offset_s=30, price=20099.75, size=20),   # at level (-0.25)
        # One trade through level (adverse for LONG: price < level)
        _trade(ts_offset_s=40, price=20099.00, size=5),    # through (-1.0 pts)
    ]

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    # at_level: 50 + 30 + 20 = 100 (within ±0.50)
    # through: 5 (price < level for LONG)
    # Note: 20099.75 is within ±0.50 of 20100 → at-level, NOT through
    # absorption = 100 / (100 + 5) = 0.9524
    assert abs(result["int_absorption_ratio"] - 100.0 / 105.0) < 0.001


def test_absorption_ratio_blowthrough():
    """Light volume at level / heavy through = low ratio."""
    fc = FeatureComputer()

    bbo_updates = [_bbo(ts_offset_s=0)]

    trades = [
        _trade(ts_offset_s=10, price=20100.00, size=5),    # at level
        _trade(ts_offset_s=20, price=20098.00, size=50),   # through (LONG: < level)
        _trade(ts_offset_s=30, price=20097.00, size=40),   # through
        _trade(ts_offset_s=40, price=20096.00, size=30),   # through
    ]

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    # at_level: 5
    # through: 50 + 40 + 30 = 120
    # absorption = 5 / (5 + 120) = 0.04
    assert abs(result["int_absorption_ratio"] - 5.0 / 125.0) < 0.001


def test_absorption_ratio_zero_through():
    """No through-level volume → ratio reflects pure absorption."""
    fc = FeatureComputer()

    bbo_updates = [_bbo(ts_offset_s=0)]

    # All trades at the level
    trades = [
        _trade(ts_offset_s=10, price=20100.00, size=50),
        _trade(ts_offset_s=20, price=20100.25, size=30),
    ]

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    # at_level: 80, through: 0
    # absorption = 80 / (80 + 0) = 1.0
    assert result["int_absorption_ratio"] == 1.0


def test_duration_calculation_last_trade():
    """Last event's duration extends to window_end."""
    fc = FeatureComputer()

    # Single BBO event at window start with mid below level
    bbo_updates = [
        _bbo(ts_offset_s=0, bid=20099.50, ask=20099.75),  # mid = 20099.625
    ]
    trades = []

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    # Only one event at t=0, mid=20099.625 < 20100 → beyond for LONG
    # Duration extends from t=0 to window_end (300s)
    assert abs(result["int_time_beyond_level"] - 300.0) < 0.01


def test_empty_trades_returns_zeros():
    """No events produces all-zero features."""
    fc = FeatureComputer()

    result = fc.compute_features(
        trades=[],
        bbo_updates=[],
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result["int_time_beyond_level"] == 0.0
    assert result["int_time_within_2pts"] == 0.0
    assert result["int_absorption_ratio"] == 0.0


def test_single_trade_at_level():
    """One trade at the level: absorption = 1.0 (all volume at level)."""
    fc = FeatureComputer()

    bbo_updates = [_bbo(ts_offset_s=0)]
    trades = [_trade(ts_offset_s=10, price=20100.00, size=10)]

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    assert result["int_absorption_ratio"] == 1.0


def test_feature_values_are_floats():
    """All returned values are Python floats, not Decimals."""
    fc = FeatureComputer()

    bbo_updates = [_bbo(ts_offset_s=0)]
    trades = [_trade(ts_offset_s=10, price=20100.00, size=5)]

    result = fc.compute_features(
        trades=trades,
        bbo_updates=bbo_updates,
        level_price=LEVEL_PRICE,
        direction=TradeDirection.LONG,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )

    for key, value in result.items():
        assert isinstance(value, float), f"{key} is {type(value)}, expected float"
