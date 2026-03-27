"""
Outcome Tracker — monitors price after predictions to determine correctness.

For each prediction, tracks MFE (Maximum Favorable Excursion) and MAE
(Maximum Adverse Excursion). Resolves predictions as correct or incorrect
based on defined thresholds matching the experiment's labeling code.

Resolution thresholds:
- MFE >= 25.0 pts → tradeable_reversal (tp_hit)
- MAE >= 37.5 pts with MFE >= 5.0 → trap_reversal (sl_hit)
- MAE >= 37.5 pts with MFE < 5.0 → aggressive_blowthrough (sl_hit)

Resolution order: MFE checked first. If both thresholds cross on the same
tick, the favorable excursion wins (optimistic for the dashboard).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.model import Prediction, ResolvedOutcome
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate

# Thresholds matching experiment/labeling.py
MFE_TARGET = 25.0
MAE_STOP = 37.5
TRAP_MFE_MIN = 5.0


@dataclass
class _ActiveTracker:
    """Internal tracker state for one prediction."""

    prediction: Prediction
    mfe: float = 0.0
    mae: float = 0.0


class OutcomeTracker:
    """Tracks prediction outcomes by monitoring price after signals.

    Processes every trade tick against all active (unresolved) predictions.
    With typically 0-3 active predictions, this is negligible load.
    """

    def __init__(self) -> None:
        self._trackers: dict[str, _ActiveTracker] = {}
        self._callbacks: list[Callable[[ResolvedOutcome], None]] = []

    def start_tracking(self, prediction: Prediction) -> None:
        """Begin tracking price for this prediction."""
        self._trackers[prediction.event_id] = _ActiveTracker(
            prediction=prediction,
        )

    def on_trade(self, trade: TradeUpdate) -> list[ResolvedOutcome]:
        """Process a trade against all active trackers.

        Returns list of any newly resolved outcomes.
        """
        resolved: list[ResolvedOutcome] = []

        for event_id in list(self._trackers.keys()):
            tracker = self._trackers.get(event_id)
            if tracker is None:
                continue

            pred = tracker.prediction
            trade_price = float(trade.price)
            level_price = float(pred.level_price)

            # Update MFE/MAE based on direction
            if pred.trade_direction == TradeDirection.LONG:
                favorable = trade_price - level_price
                adverse = level_price - trade_price
            else:  # SHORT
                favorable = level_price - trade_price
                adverse = trade_price - level_price

            tracker.mfe = max(tracker.mfe, favorable)
            tracker.mae = max(tracker.mae, adverse)

            # Check resolution (MFE first — spec ordering)
            outcome = self._check_resolution(tracker, trade.timestamp)
            if outcome is not None:
                resolved.append(outcome)

        return resolved

    def on_session_end(self, timestamp: datetime | None = None) -> list[ResolvedOutcome]:
        """Resolve all remaining unresolved predictions at session end."""
        resolved: list[ResolvedOutcome] = []
        ts = timestamp if timestamp is not None else datetime.now(UTC)

        for event_id in list(self._trackers.keys()):
            tracker = self._trackers.get(event_id)
            if tracker is None:
                continue
            outcome = self._force_resolve(tracker, ts, "session_end")
            resolved.append(outcome)

        return resolved

    def on_outcome_resolved(
        self, callback: Callable[[ResolvedOutcome], None],
    ) -> None:
        """Register callback for resolved outcomes."""
        self._callbacks.append(callback)

    @property
    def active_trackers(self) -> int:
        """Count of unresolved predictions being tracked."""
        return len(self._trackers)

    def _check_resolution(
        self, tracker: _ActiveTracker, timestamp: datetime,
    ) -> ResolvedOutcome | None:
        """Check if MFE/MAE thresholds resolve this prediction.

        MFE checked first: if both thresholds cross on the same tick,
        the favorable excursion wins.
        """
        # TP hit (MFE >= 25)
        if tracker.mfe >= MFE_TARGET:
            return self._resolve(
                tracker, timestamp, "tp_hit", "tradeable_reversal",
            )

        # SL hit (MAE >= 37.5)
        if tracker.mae >= MAE_STOP:
            actual = "trap_reversal" if tracker.mfe >= TRAP_MFE_MIN else "aggressive_blowthrough"
            return self._resolve(tracker, timestamp, "sl_hit", actual)

        return None

    def _resolve(
        self,
        tracker: _ActiveTracker,
        timestamp: datetime,
        resolution_type: str,
        actual_class: str,
    ) -> ResolvedOutcome:
        """Resolve a tracker and remove from active tracking."""
        outcome = ResolvedOutcome(
            event_id=tracker.prediction.event_id,
            prediction=tracker.prediction,
            mfe_points=tracker.mfe,
            mae_points=tracker.mae,
            resolution_type=resolution_type,
            prediction_correct=(
                tracker.prediction.predicted_class == actual_class
            ),
            actual_class=actual_class,
            resolved_at=timestamp,
        )

        # Remove from active trackers
        del self._trackers[tracker.prediction.event_id]

        # Fire callbacks
        for cb in self._callbacks:
            cb(outcome)

        return outcome

    def _force_resolve(
        self,
        tracker: _ActiveTracker,
        timestamp: datetime,
        resolution_type: str,
    ) -> ResolvedOutcome:
        """Force-resolve using current MFE/MAE state."""
        if tracker.mfe >= MFE_TARGET:
            actual = "tradeable_reversal"
        elif tracker.mae >= MAE_STOP:
            actual = (
                "trap_reversal"
                if tracker.mfe >= TRAP_MFE_MIN
                else "aggressive_blowthrough"
            )
        elif tracker.mfe >= TRAP_MFE_MIN:
            actual = "trap_reversal"
        else:
            actual = "aggressive_blowthrough"

        return self._resolve(tracker, timestamp, resolution_type, actual)
