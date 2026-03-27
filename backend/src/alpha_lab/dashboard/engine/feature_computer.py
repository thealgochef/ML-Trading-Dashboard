"""
Feature computer for the 3 validated CatBoost features.

Computes int_time_beyond_level, int_time_within_2pts, and
int_absorption_ratio from observation window tick data.

Follows the batch experiment code in src/alpha_lab/experiment/features.py:
- Tempo features (time_beyond, time_within) use mid-price from BBO events
  and durations across ALL events (trades + BBO), matching the batch code's
  use of all MBP-10 events with mid-price.
- Absorption ratio uses trade prices/volumes only, ±0.50 pts proximity
  for "at level", adverse direction for "through level", and the
  at/(at+through) formula (bounded [0, 1]).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate

# Match batch code: LEVEL_PROXIMITY_PTS = 0.50
LEVEL_PROXIMITY = Decimal("0.50")


class FeatureComputer:
    """Computes the 3 CatBoost features from observation window tick data."""

    def compute_features(
        self,
        trades: list[TradeUpdate],
        bbo_updates: list[BBOUpdate],
        level_price: Decimal,
        direction: TradeDirection,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, float]:
        """Compute the 3 features from observation window data.

        Returns:
            {
                "int_time_beyond_level": float,   # seconds
                "int_time_within_2pts": float,     # seconds
                "int_absorption_ratio": float,     # ratio [0, 1]
            }
        """
        # Tempo features: merge all events, use mid-price
        time_beyond, time_within = self._compute_tempo_features(
            trades, bbo_updates, level_price, direction,
            window_start, window_end,
        )

        # Absorption: trade volumes only
        absorption = self._compute_absorption(
            trades, level_price, direction,
        )

        return {
            "int_time_beyond_level": float(time_beyond),
            "int_time_within_2pts": float(time_within),
            "int_absorption_ratio": float(absorption),
        }

    def _compute_tempo_features(
        self,
        trades: list[TradeUpdate],
        bbo_updates: list[BBOUpdate],
        level_price: Decimal,
        direction: TradeDirection,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[float, float]:
        """Compute time_beyond_level and time_within_2pts.

        Merges trades and BBO updates into a single chronological event stream.
        For each consecutive pair of events, determines the mid-price at that
        point and accumulates duration based on the condition.
        """
        # Build chronological event stream with mid-prices
        events: list[tuple[datetime, Decimal]] = []

        # Track mid-price state from BBO updates
        current_mid: Decimal | None = None

        # Sort all events by timestamp
        all_events: list[tuple[datetime, str, object]] = []
        for bbo in bbo_updates:
            all_events.append((bbo.timestamp, "bbo", bbo))
        for trade in trades:
            all_events.append((trade.timestamp, "trade", trade))

        all_events.sort(key=lambda x: x[0])

        # Build (timestamp, mid_price) pairs
        for ts, event_type, event_obj in all_events:
            if event_type == "bbo":
                bbo_obj: BBOUpdate = event_obj  # type: ignore[assignment]
                current_mid = (bbo_obj.bid_price + bbo_obj.ask_price) / 2
            # Record event with current mid (if we have one)
            if current_mid is not None:
                events.append((ts, current_mid))

        if not events:
            return 0.0, 0.0

        level_f = float(level_price)
        time_beyond = 0.0
        time_within = 0.0

        for i in range(len(events)):
            ts_i, mid_i = events[i]
            mid_f = float(mid_i)

            # Duration: to next event, or to window_end for last event
            if i + 1 < len(events):
                duration = (events[i + 1][0] - ts_i).total_seconds()
            else:
                duration = (window_end - ts_i).total_seconds()

            if duration <= 0:
                continue

            # Time beyond level (adverse direction)
            if (direction == TradeDirection.LONG and mid_f < level_f) or (
                direction == TradeDirection.SHORT and mid_f > level_f
            ):
                time_beyond += duration

            # Time within 2 pts
            if abs(mid_f - level_f) <= 2.0:
                time_within += duration

        return time_beyond, time_within

    def _compute_absorption(
        self,
        trades: list[TradeUpdate],
        level_price: Decimal,
        direction: TradeDirection,
    ) -> float:
        """Compute absorption ratio from trade volumes.

        Matches batch code:
        - at_level: volume within ±0.50 pts of level
        - through_level: volume in adverse direction (< level for LONG, > for SHORT)
        - ratio: at / (at + through)
        """
        at_level_volume = 0
        through_volume = 0

        for trade in trades:
            distance = abs(trade.price - level_price)

            if distance <= LEVEL_PROXIMITY:
                at_level_volume += trade.size
            else:
                # Check adverse direction
                if (direction == TradeDirection.LONG and trade.price < level_price) or (
                    direction == TradeDirection.SHORT and trade.price > level_price
                ):
                    through_volume += trade.size

        total = at_level_volume + through_volume
        if total == 0:
            return 0.0

        return float(at_level_volume) / float(total)
