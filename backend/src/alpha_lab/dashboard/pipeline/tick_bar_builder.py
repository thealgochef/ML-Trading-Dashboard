"""
Streaming tick-bar builder — accumulates trades and fires callbacks.

Maintains one accumulator per configured tick count (e.g. 987, 2000).
When an accumulator's trade count hits the threshold, it finalises an
OHLCVBar and fires all registered on_bar_complete callbacks.

This component is generic — no replay or threading logic. The callback
mechanism follows the same pattern as TouchDetector.on_touch and
ObservationManager.on_observation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from alpha_lab.dashboard.pipeline.price_buffer import OHLCVBar
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate


@dataclass
class _Accumulator:
    """In-progress bar state for a single tick-count threshold."""

    tick_count: int
    count: int = 0
    open: Decimal = Decimal(0)
    high: Decimal = Decimal(0)
    low: Decimal = Decimal(0)
    close: Decimal = Decimal(0)
    volume: int = 0
    start_ts: object = None  # datetime, set on first trade in current bar
    last_ts: object = None  # datetime, set on first trade

    def reset(self) -> None:
        self.count = 0
        self.volume = 0
        self.start_ts = None
        self.last_ts = None


class TickBarBuilder:
    """Accumulates trades and fires callbacks when tick-bar thresholds are hit.

    Completed bars are stored in ``_completed_bars`` so they survive whether
    or not a WebSocket client was connected when the bar completed. Use
    ``get_bars()`` to retrieve stored bars for a given timeframe.

    Usage::

        builder = TickBarBuilder(tick_counts=[987, 2000])
        builder.on_bar_complete(my_callback)
        # ... in trade handler:
        builder.on_trade(trade)
    """

    def __init__(self, tick_counts: list[int] | None = None) -> None:
        if tick_counts is None:
            tick_counts = [987, 2000]
        self._accumulators: dict[str, _Accumulator] = {
            f"{tc}t": _Accumulator(tick_count=tc) for tc in tick_counts
        }
        self._callbacks: list[Callable[[str, OHLCVBar], None]] = []
        # Persistent storage of completed bars per timeframe
        self._completed_bars: dict[str, list[OHLCVBar]] = {
            f"{tc}t": [] for tc in tick_counts
        }

    def on_bar_complete(self, callback: Callable[[str, OHLCVBar], None]) -> None:
        """Register a callback fired when any tick bar completes.

        Callback signature: (timeframe: str, bar: OHLCVBar) -> None
        """
        self._callbacks.append(callback)

    def get_bars(self, timeframe: str, include_partial: bool = False) -> list[OHLCVBar]:
        """Return all completed bars for a timeframe.

        If *include_partial* is True, appends the in-progress bar (if any)
        so the chart always shows the latest price action even before the
        tick count threshold is reached.
        """
        bars = list(self._completed_bars.get(timeframe, []))
        if include_partial:
            acc = self._accumulators.get(timeframe)
            if acc and acc.count > 0 and acc.start_ts is not None:
                partial_ts = acc.start_ts
                # Guard rollover boundary: partial identity must not collide with
                # the immediately previous completed bar timestamp.
                if bars and partial_ts <= bars[-1].timestamp:
                    partial_ts = bars[-1].timestamp + timedelta(microseconds=1)
                bars.append(OHLCVBar(
                    timestamp=partial_ts,
                    open=acc.open,
                    high=acc.high,
                    low=acc.low,
                    close=acc.close,
                    volume=acc.volume,
                ))
        return bars

    def on_trade(self, trade: TradeUpdate) -> None:
        """Process a single trade through all accumulators."""
        for tf_key, acc in self._accumulators.items():
            acc.count += 1
            acc.volume += trade.size
            acc.close = trade.price
            acc.last_ts = trade.timestamp

            if acc.count == 1:
                acc.open = trade.price
                acc.high = trade.price
                acc.low = trade.price
                acc.start_ts = trade.timestamp
            else:
                if trade.price > acc.high:
                    acc.high = trade.price
                if trade.price < acc.low:
                    acc.low = trade.price

            if acc.count >= acc.tick_count:
                bar = OHLCVBar(
                    timestamp=acc.last_ts,
                    open=acc.open,
                    high=acc.high,
                    low=acc.low,
                    close=acc.close,
                    volume=acc.volume,
                )
                self._completed_bars[tf_key].append(bar)
                acc.reset()
                for cb in self._callbacks:
                    cb(tf_key, bar)

    def preload_historical(self, timeframe: str, bars: list[OHLCVBar]) -> None:
        """Prepend pre-built historical bars (no callbacks fired).

        Used at startup to seed the chart with recent history before live
        ticks begin flowing.  Bars are inserted at the front of the list so
        live bars can simply append as usual.
        """
        if timeframe not in self._completed_bars:
            return
        # Prepend so that live bars (if any) follow chronologically
        self._completed_bars[timeframe] = bars + self._completed_bars[timeframe]

    def reset(self) -> None:
        """Clear all accumulators and stored bars (e.g. on day boundary)."""
        for acc in self._accumulators.values():
            acc.reset()
        for bars in self._completed_bars.values():
            bars.clear()
