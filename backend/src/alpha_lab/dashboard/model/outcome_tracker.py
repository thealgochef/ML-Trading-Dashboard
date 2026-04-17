"""
Outcome Tracker — monitors price after predictions to determine correctness.

For each prediction, tracks MFE (Maximum Favorable Excursion) and MAE
(Maximum Adverse Excursion). Resolves predictions as correct or incorrect
based on defined thresholds matching the experiment's labeling code.

Resolution thresholds (configurable, default aligned to training TP=15/SL=30):
- MFE >= mfe_target (default 15.0) pts → tradeable_reversal (tp_hit)
- MAE >= mae_stop (default 30.0) pts with MFE >= trap_mfe_min → trap_reversal (sl_hit)
- MAE >= mae_stop pts with MFE < trap_mfe_min → aggressive_blowthrough (sl_hit)

Resolution order: MAE checked first (conservative). If both thresholds
cross on the same tick, the adverse excursion wins. This matches the
training label logic in experiment/labeling.py which also checks MAE first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.model import Prediction, ResolvedOutcome
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate

# Default thresholds (aligned to Quant-Lab utility training: TP=15, SL=30)
MFE_TARGET = 15.0
MAE_STOP = 30.0
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

    def __init__(
        self,
        mfe_target: float = MFE_TARGET,
        mae_stop: float = MAE_STOP,
        trap_mfe_min: float = TRAP_MFE_MIN,
    ) -> None:
        self._mfe_target = mfe_target
        self._mae_stop = mae_stop
        self._trap_mfe_min = trap_mfe_min
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

            # Check resolution (MAE first — conservative, matches training labels)
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

        MAE checked first (conservative): if both thresholds cross on the
        same tick, the adverse excursion wins.  This matches the training
        label logic in experiment/labeling.py.
        """
        # SL hit — check adverse first (conservative)
        if tracker.mae >= self._mae_stop:
            actual = "trap_reversal" if tracker.mfe >= self._trap_mfe_min else "aggressive_blowthrough"
            return self._resolve(tracker, timestamp, "sl_hit", actual)

        # TP hit
        if tracker.mfe >= self._mfe_target:
            return self._resolve(
                tracker, timestamp, "tp_hit", "tradeable_reversal",
            )

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
        """Force-resolve using current MFE/MAE state (MAE-first ordering)."""
        if tracker.mae >= self._mae_stop:
            actual = (
                "trap_reversal"
                if tracker.mfe >= self._trap_mfe_min
                else "aggressive_blowthrough"
            )
        elif tracker.mfe >= self._mfe_target:
            actual = "tradeable_reversal"
        elif tracker.mfe >= self._trap_mfe_min:
            actual = "trap_reversal"
        else:
            actual = "aggressive_blowthrough"

        return self._resolve(tracker, timestamp, resolution_type, actual)
