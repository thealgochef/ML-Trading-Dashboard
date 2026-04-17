"""
In-memory rolling price buffer for real-time chart rendering.

Maintains a thread-safe rolling window of recent trades and BBO updates.
Provides methods to query recent data and construct OHLCV candles at
arbitrary timeframes. Does not persist to disk — ephemeral working memory.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate

# Tick-bar timeframe string → tick count
_TICK_COUNTS: dict[str, int] = {
    "147t": 147,
    "987t": 987,
    "2000t": 2000,
}

# Timeframe string → timedelta
_TIMEFRAME_MAP: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "10m": timedelta(minutes=10),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1H": timedelta(hours=1),
    "4H": timedelta(hours=4),
    "1D": timedelta(days=1),
}


@dataclass(frozen=True, slots=True)
class OHLCVBar:
    """A single OHLCV candle."""

    timestamp: datetime  # Start of the bar period
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class PriceBuffer:
    """Thread-safe in-memory buffer of recent tick data."""

    def __init__(self, max_duration: timedelta = timedelta(hours=48)) -> None:
        self._max_duration = max_duration
        self._trades: deque[TradeUpdate] = deque()
        self._bbo_history: deque[BBOUpdate] = deque()
        self._latest_bbo: BBOUpdate | None = None
        self._lock = threading.Lock()

        # Historical OHLCV bars (1m resolution) loaded on startup
        self._historical_bars: list[OHLCVBar] = []

    # ── Write interface ───────────────────────────────────────────

    def load_historical(self, bars: list[OHLCVBar]) -> None:
        """Load pre-built historical OHLCV bars (1m resolution).

        These are merged with live-computed bars in get_ohlcv().
        Should be called once on startup after fetching from data vendor.
        """
        with self._lock:
            self._historical_bars = sorted(bars, key=lambda b: b.timestamp)

    def add_trade(self, trade: TradeUpdate) -> None:
        with self._lock:
            self._trades.append(trade)

    def add_bbo(self, bbo: BBOUpdate) -> None:
        with self._lock:
            self._latest_bbo = bbo
            self._bbo_history.append(bbo)

    # ── Read interface ────────────────────────────────────────────

    @property
    def latest_price(self) -> Decimal | None:
        with self._lock:
            if self._trades:
                return self._trades[-1].price
            return None

    @property
    def latest_bid(self) -> Decimal | None:
        with self._lock:
            return self._latest_bbo.bid_price if self._latest_bbo else None

    @property
    def latest_ask(self) -> Decimal | None:
        with self._lock:
            return self._latest_bbo.ask_price if self._latest_bbo else None

    @property
    def latest_mid(self) -> Decimal | None:
        with self._lock:
            if self._latest_bbo:
                return (self._latest_bbo.bid_price + self._latest_bbo.ask_price) / 2
            return None

    def get_trades_since(self, since: datetime) -> list[TradeUpdate]:
        with self._lock:
            return [t for t in self._trades if t.timestamp >= since]

    def get_high_low_in_range(
        self, start: datetime, end: datetime
    ) -> tuple[Decimal, Decimal] | None:
        """Get the highest high and lowest low in a time range.

        Checks both historical 1m bars and live trades. Returns (high, low)
        or None if no data exists in the range.
        """
        highs: list[Decimal] = []
        lows: list[Decimal] = []

        with self._lock:
            # From historical OHLCV bars
            for bar in self._historical_bars:
                if start <= bar.timestamp < end:
                    highs.append(bar.high)
                    lows.append(bar.low)

            # From live trades
            for trade in self._trades:
                if start <= trade.timestamp < end:
                    highs.append(trade.price)
                    lows.append(trade.price)

        if not highs:
            return None
        return max(highs), min(lows)

    def get_ohlcv(self, timeframe: str, since: datetime) -> list[OHLCVBar]:
        """Build OHLCV bars from historical backfill + live trades.

        For tick-bar timeframes (987t, 2000t): groups sequential trades by
        count. No historical bar merge — tick bars require raw trades.

        For time-based timeframes (1m–1D): historical bars (1m) are merged
        with live-computed bars. For timeframes > 1m, 1m bars are aggregated.
        """
        # Tick-bar path: group by trade count, no historical merge
        tick_count = _TICK_COUNTS.get(timeframe)
        if tick_count is not None:
            return self._build_tick_bars(tick_count, since)

        td = _TIMEFRAME_MAP.get(timeframe)
        if td is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")

        # 1. Build live bars from trades at 1m resolution first,
        #    then aggregate to requested timeframe
        live_bars_1m = self._build_bars_from_trades(_TIMEFRAME_MAP["1m"], since)

        # 2. Get historical 1m bars in the time range
        with self._lock:
            hist_bars = [b for b in self._historical_bars if b.timestamp >= since]

        # 3. Merge: live 1m bars override historical for overlapping minutes
        live_timestamps = {b.timestamp for b in live_bars_1m}
        merged_1m = [b for b in hist_bars if b.timestamp not in live_timestamps]
        merged_1m.extend(live_bars_1m)
        merged_1m.sort(key=lambda b: b.timestamp)

        if not merged_1m:
            return []

        # 4. If timeframe is 1m, return merged directly
        if timeframe == "1m":
            return merged_1m

        # 5. Aggregate 1m bars into the requested timeframe
        return _aggregate_bars(merged_1m, td)

    def _build_bars_from_trades(
        self, td: timedelta, since: datetime
    ) -> list[OHLCVBar]:
        """Build OHLCV bars from raw trade data at the given resolution."""
        with self._lock:
            trades = [t for t in self._trades if t.timestamp >= since]

        if not trades:
            return []

        td_seconds = td.total_seconds()
        buckets: dict[datetime, list[TradeUpdate]] = {}
        for trade in trades:
            epoch = trade.timestamp.timestamp()
            bar_start_epoch = (epoch // td_seconds) * td_seconds
            bar_start = datetime.fromtimestamp(
                bar_start_epoch, tz=trade.timestamp.tzinfo
            )
            if bar_start not in buckets:
                buckets[bar_start] = []
            buckets[bar_start].append(trade)

        bars: list[OHLCVBar] = []
        for bar_ts in sorted(buckets):
            bucket_trades = buckets[bar_ts]
            prices = [t.price for t in bucket_trades]
            volume = sum(t.size for t in bucket_trades)
            bars.append(OHLCVBar(
                timestamp=bar_ts,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=volume,
            ))
        return bars

    def _build_tick_bars(self, tick_count: int, since: datetime) -> list[OHLCVBar]:
        """Build OHLCV bars by grouping sequential trades by count.

        Each bar contains exactly ``tick_count`` trades. The final partial
        group (in-progress bar) is always included so the chart is never
        empty while trades are flowing. The frontend's 5-second polling
        naturally updates this bar until it completes.
        """
        with self._lock:
            trades = [t for t in self._trades if t.timestamp >= since]

        if not trades:
            return []

        bars: list[OHLCVBar] = []
        n = len(trades)
        full_bars = n // tick_count
        remainder = n % tick_count

        for group_idx in range(full_bars):
            start = group_idx * tick_count
            end = start + tick_count
            chunk = trades[start:end]
            prices = [t.price for t in chunk]
            bars.append(OHLCVBar(
                timestamp=chunk[-1].timestamp,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=sum(t.size for t in chunk),
            ))

        # Always include the in-progress partial bar so the chart
        # shows current price action even before tick_count is reached.
        if remainder > 0:
            start = full_bars * tick_count
            chunk = trades[start:]
            prices = [t.price for t in chunk]
            bars.append(OHLCVBar(
                timestamp=chunk[-1].timestamp,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=sum(t.size for t in chunk),
            ))

        return bars

    # ── Maintenance ───────────────────────────────────────────────

    def get_trades_in_range(
        self, start: datetime, end: datetime,
    ) -> list[TradeUpdate]:
        """Return trades with timestamps in [start, end)."""
        with self._lock:
            return [
                t for t in self._trades
                if start <= t.timestamp < end
            ]

    def get_bbo_in_range(
        self, start: datetime, end: datetime,
    ) -> list[BBOUpdate]:
        """Return BBO updates with timestamps in [start, end)."""
        with self._lock:
            return [
                b for b in self._bbo_history
                if start <= b.timestamp < end
            ]

    def evict(self) -> None:
        """Remove data older than max_duration."""
        with self._lock:
            if not self._trades:
                return
            cutoff = self._trades[-1].timestamp - self._max_duration
            while self._trades and self._trades[0].timestamp < cutoff:
                self._trades.popleft()
            while self._bbo_history and self._bbo_history[0].timestamp < cutoff:
                self._bbo_history.popleft()


def _aggregate_bars(bars_1m: list[OHLCVBar], td: timedelta) -> list[OHLCVBar]:
    """Aggregate 1m bars into a larger timeframe."""
    if not bars_1m:
        return []

    td_seconds = td.total_seconds()
    buckets: dict[datetime, list[OHLCVBar]] = {}

    for bar in bars_1m:
        epoch = bar.timestamp.timestamp()
        bar_start_epoch = (epoch // td_seconds) * td_seconds
        bar_start = datetime.fromtimestamp(bar_start_epoch, tz=bar.timestamp.tzinfo)
        if bar_start not in buckets:
            buckets[bar_start] = []
        buckets[bar_start].append(bar)

    result: list[OHLCVBar] = []
    for bar_ts in sorted(buckets):
        group = buckets[bar_ts]
        result.append(OHLCVBar(
            timestamp=bar_ts,
            open=group[0].open,
            high=max(b.high for b in group),
            low=min(b.low for b in group),
            close=group[-1].close,
            volume=sum(b.volume for b in group),
        ))
    return result
