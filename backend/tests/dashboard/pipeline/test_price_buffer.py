"""
Phase 1 — Price Buffer Tests

Tests the in-memory rolling price buffer that provides recent tick data for
chart rendering and level monitoring. This is ephemeral working memory —
it does not persist to disk.

Business context: The dashboard loads with 48 hours of historical data and
shows real-time updates at 1-second refresh. The price buffer holds recent
ticks in memory for fast access. Later phases will use it for level touch
detection and feature computation.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_lab.dashboard.pipeline.price_buffer import OHLCVBar, PriceBuffer
from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate


def _trade(
    ts: datetime | None = None,
    price: float = 20100.25,
    size: int = 3,
    side: str = "BUY",
    symbol: str = "NQH6",
) -> TradeUpdate:
    if ts is None:
        ts = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side=side,
        symbol=symbol,
    )


def _bbo(
    ts: datetime | None = None,
    bid: float = 20100.00,
    ask: float = 20100.25,
) -> BBOUpdate:
    if ts is None:
        ts = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    return BBOUpdate(
        timestamp=ts,
        bid_price=Decimal(str(bid)),
        bid_size=15,
        ask_price=Decimal(str(ask)),
        ask_size=12,
        symbol="NQH6",
    )


def test_add_and_retrieve_trade():
    """Added trades are retrievable via get_trades_since()."""
    buf = PriceBuffer()
    ts = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    buf.add_trade(_trade(ts=ts))

    trades = buf.get_trades_since(ts - timedelta(seconds=1))
    assert len(trades) == 1
    assert trades[0].price == Decimal("20100.25")


def test_latest_price():
    """latest_price returns the most recent trade price."""
    buf = PriceBuffer()
    assert buf.latest_price is None

    buf.add_trade(_trade(price=20100.00))
    assert buf.latest_price == Decimal("20100.00")

    buf.add_trade(_trade(price=20105.50))
    assert buf.latest_price == Decimal("20105.50")


def test_latest_bid_ask_mid():
    """BBO updates correctly set latest bid, ask, and mid-price."""
    buf = PriceBuffer()
    assert buf.latest_bid is None
    assert buf.latest_ask is None
    assert buf.latest_mid is None

    buf.add_bbo(_bbo(bid=20100.00, ask=20100.25))
    assert buf.latest_bid == Decimal("20100.00")
    assert buf.latest_ask == Decimal("20100.25")
    assert buf.latest_mid == Decimal("20100.125")


def test_eviction_by_age():
    """Data older than max_duration is cleaned up."""
    buf = PriceBuffer(max_duration=timedelta(hours=1))
    now = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # Add an old trade (2 hours ago)
    buf.add_trade(_trade(ts=now - timedelta(hours=2)))
    # Add a recent trade
    buf.add_trade(_trade(ts=now))

    # Trigger eviction
    buf.evict()

    trades = buf.get_trades_since(now - timedelta(hours=3))
    assert len(trades) == 1
    assert trades[0].timestamp == now


def test_ohlcv_1m_construction():
    """get_ohlcv('1m', ...) produces correct 1-minute candles."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # Add several trades within the same 1-minute bar
    buf.add_trade(_trade(ts=base, price=20100.00, size=10))
    buf.add_trade(_trade(ts=base + timedelta(seconds=15), price=20105.00, size=5))
    buf.add_trade(_trade(ts=base + timedelta(seconds=30), price=20098.00, size=8))
    buf.add_trade(_trade(ts=base + timedelta(seconds=45), price=20102.00, size=3))

    bars = buf.get_ohlcv("1m", base - timedelta(seconds=1))
    assert len(bars) == 1
    bar = bars[0]
    assert isinstance(bar, OHLCVBar)
    assert bar.open == Decimal("20100.00")
    assert bar.high == Decimal("20105.00")
    assert bar.low == Decimal("20098.00")
    assert bar.close == Decimal("20102.00")
    assert bar.volume == 26  # 10 + 5 + 8 + 3


def test_ohlcv_5m_construction():
    """5-minute candles aggregate correctly."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # Trades across 6 minutes — should produce 2 bars at 5m
    for i in range(6):
        ts = base + timedelta(minutes=i)
        buf.add_trade(_trade(ts=ts, price=20100.00 + i, size=1))

    bars = buf.get_ohlcv("5m", base - timedelta(seconds=1))
    # 14:30-14:34 → bar 1, 14:35 → bar 2
    assert len(bars) == 2


def test_ohlcv_empty_period():
    """Timeframes with no trades produce no bars (not bars with zero volume)."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    bars = buf.get_ohlcv("1m", base)
    assert len(bars) == 0


def test_ohlcv_single_trade_in_bar():
    """A bar with one trade has open=high=low=close."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    buf.add_trade(_trade(ts=base, price=20100.00, size=5))

    bars = buf.get_ohlcv("1m", base - timedelta(seconds=1))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.open == bar.high == bar.low == bar.close == Decimal("20100.00")
    assert bar.volume == 5


def test_thread_safety():
    """Concurrent reads and writes do not raise exceptions or produce corrupt data."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    errors: list[Exception] = []

    def writer():
        try:
            for i in range(100):
                ts = base + timedelta(milliseconds=i)
                buf.add_trade(_trade(ts=ts, price=20100.0 + i, size=1))
        except Exception as e:
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                buf.get_trades_since(base - timedelta(hours=1))
                buf.get_ohlcv("1m", base - timedelta(hours=1))
                _ = buf.latest_price
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0


def test_ohlcv_uses_trades_only():
    """BBO updates do not affect OHLCV candle prices."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # Add BBO but no trade
    buf.add_bbo(_bbo(ts=base, bid=20100.00, ask=20100.25))

    bars = buf.get_ohlcv("1m", base - timedelta(seconds=1))
    assert len(bars) == 0  # BBO should not create a bar

    # Now add a trade
    buf.add_trade(_trade(ts=base, price=20100.00, size=5))
    bars = buf.get_ohlcv("1m", base - timedelta(seconds=1))
    assert len(bars) == 1
    assert bars[0].open == Decimal("20100.00")


# ── Tick Bar Tests ────────────────────────────────────────────────


def _feed_trades(buf: PriceBuffer, count: int, base: datetime) -> None:
    """Feed `count` trades with incrementing timestamps and varying prices."""
    for i in range(count):
        ts = base + timedelta(milliseconds=i * 10)
        # Cycle prices to create non-trivial OHLCV
        price = 20100.00 + (i % 7) * 0.25
        buf.add_trade(_trade(ts=ts, price=price, size=1 + (i % 3)))


def test_tick_bar_987_single_complete_bar():
    """Feeding exactly 987 trades produces exactly 1 tick bar."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    _feed_trades(buf, 987, base)

    bars = buf.get_ohlcv("987t", base - timedelta(seconds=1))
    assert len(bars) == 1


def test_tick_bar_2000_construction():
    """Feeding exactly 2000 trades produces exactly 1 tick bar at 2000t."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    _feed_trades(buf, 2000, base)

    bars = buf.get_ohlcv("2000t", base - timedelta(seconds=1))
    assert len(bars) == 1


def test_tick_bar_multiple_bars():
    """2500 trades at 987t → 2 complete bars + partial (526 ticks > 50%)."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    _feed_trades(buf, 2500, base)

    bars = buf.get_ohlcv("987t", base - timedelta(seconds=1))
    # 2500 / 987 = 2 full bars + 526 remainder (526/987 = 53.3% > 50%)
    assert len(bars) == 3


def test_tick_bar_ohlcv_values():
    """Tick bar OHLCV: open=first, close=last, high=max, low=min, volume=sum."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # Feed 5 trades with known prices (use tiny tick_count via 987t workaround:
    # feed exactly 987 trades with controlled prices)
    prices = [20100.00, 20105.00, 20098.00, 20102.00, 20101.00]
    sizes = [2, 3, 1, 4, 5]

    # We'll feed 987 trades total. First 5 have known prices, rest are filler.
    for i in range(987):
        ts = base + timedelta(milliseconds=i * 10)
        if i < len(prices):
            buf.add_trade(_trade(ts=ts, price=prices[i], size=sizes[i]))
        else:
            # Filler trades within the OHLCV range
            buf.add_trade(_trade(ts=ts, price=20101.00, size=1))

    bars = buf.get_ohlcv("987t", base - timedelta(seconds=1))
    assert len(bars) == 1
    bar = bars[0]
    assert bar.open == Decimal("20100.00")
    assert bar.high == Decimal("20105.00")
    assert bar.low == Decimal("20098.00")
    assert bar.close == Decimal("20101.00")
    # volume = 2+3+1+4+5 + 982*1 = 997
    assert bar.volume == 2 + 3 + 1 + 4 + 5 + 982


def test_tick_bar_timestamp_is_last_tick():
    """Each tick bar's timestamp equals the last tick's timestamp in that bar."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    _feed_trades(buf, 987, base)

    bars = buf.get_ohlcv("987t", base - timedelta(seconds=1))
    assert len(bars) == 1
    # Last tick is at index 986 → base + 986*10ms
    expected_ts = base + timedelta(milliseconds=986 * 10)
    assert bars[0].timestamp == expected_ts


def test_tick_bar_partial_included():
    """Partial final bar >50% of tick_count is included."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    # 987 + 494 = 1481 trades → 1 full bar + 494 remainder (494/987 = 50.05%)
    _feed_trades(buf, 987 + 494, base)

    bars = buf.get_ohlcv("987t", base - timedelta(seconds=1))
    assert len(bars) == 2  # full + partial included


def test_tick_bar_partial_always_included():
    """In-progress partial bar is always included regardless of size."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    # 987 + 493 = 1480 trades → 1 full bar + 493 in-progress
    _feed_trades(buf, 987 + 493, base)

    bars = buf.get_ohlcv("987t", base - timedelta(seconds=1))
    assert len(bars) == 2  # full + in-progress


def test_tick_bar_since_filter():
    """The `since` parameter correctly filters ticks before building bars."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    # Feed 2000 trades total
    _feed_trades(buf, 2000, base)

    # Ask for bars only from halfway through
    midpoint = base + timedelta(milliseconds=1000 * 10)
    bars = buf.get_ohlcv("987t", midpoint)
    # Only ~1000 trades after midpoint → 1 full bar + 13 in-progress
    assert len(bars) == 2


def test_tick_bar_cold_start():
    """Chart shows data immediately even with just a few trades (cold start)."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)

    # Just 5 trades — way below 987 threshold
    for i in range(5):
        ts = base + timedelta(milliseconds=i * 100)
        buf.add_trade(_trade(ts=ts, price=20100.0 + i * 0.25, size=1))

    bars = buf.get_ohlcv("987t", base - timedelta(seconds=1))
    assert len(bars) == 1  # In-progress bar shown
    assert bars[0].open == Decimal("20100.0")
    assert bars[0].close == Decimal("20101.0")
    assert bars[0].volume == 5


def test_tick_bar_single_trade():
    """Even a single trade produces a visible in-progress bar."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    buf.add_trade(_trade(ts=base, price=20100.00, size=3))

    bars = buf.get_ohlcv("987t", base - timedelta(seconds=1))
    assert len(bars) == 1
    assert bars[0].open == bars[0].close == Decimal("20100.00")
    assert bars[0].volume == 3


def test_tick_bar_time_based_unchanged():
    """Existing time-based '1m' path still works after tick bar addition."""
    buf = PriceBuffer()
    base = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)
    buf.add_trade(_trade(ts=base, price=20100.00, size=10))
    buf.add_trade(_trade(ts=base + timedelta(seconds=30), price=20105.00, size=5))

    bars = buf.get_ohlcv("1m", base - timedelta(seconds=1))
    assert len(bars) == 1
    assert bars[0].open == Decimal("20100.00")
    assert bars[0].close == Decimal("20105.00")
