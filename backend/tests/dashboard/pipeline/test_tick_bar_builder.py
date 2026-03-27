"""
Phase B — TickBarBuilder Tests

Tests the streaming tick-bar builder that accumulates trades and fires
on_bar_complete callbacks when a tick-count threshold is reached.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_lab.dashboard.pipeline.price_buffer import OHLCVBar
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate
from alpha_lab.dashboard.pipeline.tick_bar_builder import TickBarBuilder


def _trade(
    ts: datetime | None = None,
    price: float = 20100.25,
    size: int = 1,
) -> TradeUpdate:
    if ts is None:
        ts = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side="BUY",
        symbol="NQH6",
    )


def _feed(builder: TickBarBuilder, count: int, base: datetime) -> None:
    """Feed `count` trades with incrementing timestamps."""
    for i in range(count):
        ts = base + timedelta(milliseconds=i * 10)
        price = 20100.00 + (i % 5) * 0.25
        builder.on_trade(_trade(ts=ts, price=price, size=1 + (i % 3)))


def test_fires_on_987_complete():
    """Callback fires exactly once after 987 trades."""
    builder = TickBarBuilder(tick_counts=[987])
    results: list[tuple[str, OHLCVBar]] = []
    builder.on_bar_complete(lambda tf, bar: results.append((tf, bar)))

    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    _feed(builder, 987, base)

    assert len(results) == 1
    assert results[0][0] == "987t"
    assert isinstance(results[0][1], OHLCVBar)


def test_fires_on_2000_complete():
    """Callback fires exactly once after 2000 trades."""
    builder = TickBarBuilder(tick_counts=[2000])
    results: list[tuple[str, OHLCVBar]] = []
    builder.on_bar_complete(lambda tf, bar: results.append((tf, bar)))

    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    _feed(builder, 2000, base)

    assert len(results) == 1
    assert results[0][0] == "2000t"


def test_tracks_both_simultaneously():
    """With [987, 2000], feeding 2000 trades fires 987t twice and 2000t once."""
    builder = TickBarBuilder(tick_counts=[987, 2000])
    results: list[tuple[str, OHLCVBar]] = []
    builder.on_bar_complete(lambda tf, bar: results.append((tf, bar)))

    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    _feed(builder, 2000, base)

    tf_987 = [r for r in results if r[0] == "987t"]
    tf_2000 = [r for r in results if r[0] == "2000t"]
    assert len(tf_987) == 2   # 2000 // 987 = 2 complete bars
    assert len(tf_2000) == 1  # 2000 // 2000 = 1 complete bar


def test_ohlcv_correct():
    """Bar OHLCV values match the input trades."""
    builder = TickBarBuilder(tick_counts=[5])
    results: list[tuple[str, OHLCVBar]] = []
    builder.on_bar_complete(lambda tf, bar: results.append((tf, bar)))

    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    trades = [
        _trade(ts=base, price=100.0, size=2),
        _trade(ts=base + timedelta(seconds=1), price=105.0, size=3),
        _trade(ts=base + timedelta(seconds=2), price=98.0, size=1),
        _trade(ts=base + timedelta(seconds=3), price=102.0, size=4),
        _trade(ts=base + timedelta(seconds=4), price=101.0, size=5),
    ]
    for t in trades:
        builder.on_trade(t)

    assert len(results) == 1
    bar = results[0][1]
    assert bar.open == Decimal("100.0")
    assert bar.high == Decimal("105.0")
    assert bar.low == Decimal("98.0")
    assert bar.close == Decimal("101.0")
    assert bar.volume == 15  # 2+3+1+4+5
    assert bar.timestamp == base + timedelta(seconds=4)


def test_no_fire_before_threshold():
    """No callback fires when trade count < tick_count."""
    builder = TickBarBuilder(tick_counts=[987])
    results: list[tuple[str, OHLCVBar]] = []
    builder.on_bar_complete(lambda tf, bar: results.append((tf, bar)))

    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    _feed(builder, 986, base)

    assert len(results) == 0


def test_reset_clears_state():
    """reset() mid-accumulation clears state; next bar starts fresh."""
    builder = TickBarBuilder(tick_counts=[10])
    results: list[tuple[str, OHLCVBar]] = []
    builder.on_bar_complete(lambda tf, bar: results.append((tf, bar)))

    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # Feed 7 trades (below threshold)
    _feed(builder, 7, base)
    assert len(results) == 0

    # Reset mid-accumulation
    builder.reset()

    # Feed 10 more — should fire once (fresh start, not carry over 7)
    _feed(builder, 10, base + timedelta(seconds=10))
    assert len(results) == 1
    # The bar's open should come from the post-reset trades
    bar = results[0][1]
    assert bar.timestamp == base + timedelta(seconds=10) + timedelta(milliseconds=9 * 10)
