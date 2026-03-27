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

    def __init__(self, feature_computer: FeatureComputer) -> None:
        self._fc = feature_computer
        self._active: ObservationWindow | None = None
        self._callbacks: list[Callable[[ObservationWindow], None]] = []

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
            return None

        window = ObservationWindow(
            event=event,
            start_time=event.timestamp,
            end_time=event.timestamp + OBSERVATION_DURATION,
            status=ObservationStatus.ACTIVE,
        )
        self._active = window
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

    def _complete_window(self) -> None:
        """Complete the active window: compute features and fire callbacks."""
        window = self._active
        self._active = None

        features = self._fc.compute_features(
            trades=window.trades_accumulated,
            bbo_updates=window.bbo_accumulated,
            level_price=window.event.level_zone.representative_price,
            direction=window.event.trade_direction,
            window_start=window.start_time,
            window_end=window.end_time,
        )
        window.features = features
        window.status = ObservationStatus.COMPLETED

        logger.info(
            "Observation completed: event=%s, trades_accumulated=%d, bbo_accumulated=%d, features=%s",
            window.event.event_id[:8],
            len(window.trades_accumulated),
            len(window.bbo_accumulated),
            {k: round(v, 4) for k, v in features.items()} if features else None,
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
