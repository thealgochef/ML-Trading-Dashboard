# ruff: noqa: E402, E501
"""
Observation Manager — 5-minute observation window lifecycle.

Opens a window on TouchEvent, accumulates trades and BBO updates for 5 minutes,
then closes the window and computes features via FeatureComputer. Handles
feed drops (discard incomplete windows) and level deletions.

The model was trained on complete 5-minute windows only, so incomplete windows
(feed drops, level deletions) are discarded without feature computation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

logger = logging.getLogger(__name__)

from alpha_lab.dashboard.engine.approach_feature_computer import ApproachFeatureComputer
from alpha_lab.dashboard.engine.feature_computer import FeatureComputer
from alpha_lab.dashboard.engine.models import (
    ObservationStatus,
    ObservationWindow,
    TouchEvent,
)
from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    TradeUpdate,
)

OBSERVATION_DURATION = timedelta(minutes=5)


class ObservationManager:
    """Manages 5-minute observation windows triggered by touch events.

    Only one observation can be active at a time. Trades and BBO updates
    are accumulated during the window. Completion is checked on each
    incoming trade (event-driven, no timers needed).
    """

    def __init__(
        self,
        feature_computer: FeatureComputer,
        approach_computer: ApproachFeatureComputer | None = None,
        price_buffer=None,
        approach_window_minutes: int = 30,
    ) -> None:
        self._fc = feature_computer
        self._afc = approach_computer
        self._price_buffer = price_buffer
        self._approach_minutes = approach_window_minutes
        self._active: ObservationWindow | None = None
        self._callbacks: list[Callable[[ObservationWindow], None]] = []
        # Additive instrumentation for observation-censoring analytics
        self._accepted_touches: int = 0
        self._rejected_touches: int = 0
        self._rejection_records: list[dict[str, str]] = []

    @property
    def active_observation(self) -> ObservationWindow | None:
        return self._active

    def start_observation(self, event: TouchEvent) -> ObservationWindow | None:
        """Open a new observation window for a touch event.

        Returns None if an observation is already active.
        """
        if self._active is not None:
            logger.info(
                "Observation rejected: already active (event=%s), ignoring new touch event=%s",
                self._active.event.event_id[:8], event.event_id[:8],
            )
            self._rejected_touches += 1
            level_type = None
            if event.level_zone.levels:
                level_type = event.level_zone.levels[0].level_type.value
            self._rejection_records.append({
                "reason": "already_active",
                "session": event.session,
                "level_type": level_type or "unknown",
                "direction": event.trade_direction.value,
            })
            return None

        window = ObservationWindow(
            event=event,
            start_time=event.timestamp,
            end_time=event.timestamp + OBSERVATION_DURATION,
            status=ObservationStatus.ACTIVE,
        )
        self._active = window
        self._accepted_touches += 1
        logger.info(
            "Observation started: event=%s, direction=%s, level=%.2f, ends=%s",
            event.event_id[:8], event.trade_direction.value,
            float(event.level_zone.representative_price),
            window.end_time.isoformat(),
        )
        return window

    def on_trade(self, trade: TradeUpdate) -> None:
        """Process an incoming trade during an active observation."""
        if self._active is None:
            return

        # Check if this trade is past the window end
        if trade.timestamp > self._active.end_time:
            self._complete_window()
            return

        self._active.trades_accumulated.append(trade)

    def on_bbo(self, bbo: BBOUpdate) -> None:
        """Process an incoming BBO update during an active observation."""
        if self._active is None:
            return

        if bbo.timestamp > self._active.end_time:
            return

        self._active.bbo_accumulated.append(bbo)

    def on_connection_status(self, status: ConnectionStatus) -> None:
        """Handle connection status changes.

        RECONNECTING or DISCONNECTED status discards the active window
        because incomplete tick data produces unreliable features.
        """
        if self._active is None:
            return

        if status in (ConnectionStatus.RECONNECTING, ConnectionStatus.DISCONNECTED):
            self._discard_window(ObservationStatus.DISCARDED_FEED_DROP)

    def on_level_deleted(self, level_price: Decimal) -> None:
        """Handle deletion of a level zone.

        If the deleted level matches the active observation's zone,
        discard the window.
        """
        if self._active is None:
            return

        if self._active.event.level_zone.representative_price == level_price:
            self._discard_window(ObservationStatus.DISCARDED_LEVEL_DELETED)

    def on_observation_complete(
        self, callback: Callable[[ObservationWindow], None]
    ) -> None:
        """Register a callback for when an observation completes or is discarded."""
        self._callbacks.append(callback)


    def get_censoring_stats(self) -> dict:
        """Return additive observation censoring metrics for analytics."""
        grouped: dict[tuple[str, str, str, str], int] = {}
        for rec in self._rejection_records:
            key = (
                rec.get("reason", "unknown"),
                rec.get("session", "unknown"),
                rec.get("level_type", "unknown"),
                rec.get("direction", "unknown"),
            )
            grouped[key] = grouped.get(key, 0) + 1

        by_group = [
            {
                "reason": k[0],
                "session": k[1],
                "level_type": k[2],
                "direction": k[3],
                "count": v,
            }
            for k, v in sorted(grouped.items())
        ]

        total = self._accepted_touches + self._rejected_touches
        rejection_rate = self._rejected_touches / total if total else 0.0

        return {
            "summary": {
                "accepted_touches": self._accepted_touches,
                "rejected_touches": self._rejected_touches,
                "rejection_rate": rejection_rate,
            },
            "by_group": by_group,
            "records": list(self._rejection_records),
        }

    def _complete_window(self) -> None:
        """Complete the active window: compute features and fire callbacks."""
        window = self._active
        self._active = None

        # 1. Interaction features (post-touch, 5-min window)
        features = self._fc.compute_features(
            trades=window.trades_accumulated,
            bbo_updates=window.bbo_accumulated,
            level_price=window.event.level_zone.representative_price,
            direction=window.event.trade_direction,
            window_start=window.start_time,
            window_end=window.end_time,
        )

        # 2. Approach features (pre-touch, backward-looking)
        if self._afc is not None and self._price_buffer is not None:
            touch_ts = window.event.timestamp
            approach_start = touch_ts - timedelta(minutes=self._approach_minutes)
            approach_trades = self._price_buffer.get_trades_in_range(
                approach_start, touch_ts,
            )
            approach_bbo = self._price_buffer.get_bbo_in_range(
                approach_start, touch_ts,
            )
            approach_features = self._afc.compute_features(
                trades=approach_trades,
                bbo_updates=approach_bbo,
                approach_start=approach_start,
                approach_end=touch_ts,
            )
            features.update(approach_features)

        window.features = features
        window.status = ObservationStatus.COMPLETED

        logger.info(
            "Observation completed: event=%s, trades=%d, bbo=%d, features=%d (%s)",
            window.event.event_id[:8],
            len(window.trades_accumulated),
            len(window.bbo_accumulated),
            len(features) if features else 0,
            ", ".join(f"{k}={v:.4f}" for k, v in list(features.items())[:3]) if features else "none",
        )

        for cb in self._callbacks:
            cb(window)

    def _discard_window(self, status: ObservationStatus) -> None:
        """Discard the active window without computing features."""
        window = self._active
        self._active = None

        window.status = status
        window.features = None

        logger.info(
            "Observation discarded: event=%s, reason=%s",
            window.event.event_id[:8], status.value,
        )

        for cb in self._callbacks:
            cb(window)
