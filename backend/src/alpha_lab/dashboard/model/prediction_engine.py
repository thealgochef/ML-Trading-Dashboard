"""
Prediction Engine — CatBoost inference on observation window features.

Receives completed ObservationWindow objects from the observation manager,
extracts the 3 features, runs model.predict() and model.predict_proba(),
and produces a Prediction object.

Predictions are generated for ALL sessions (Asia, London, Pre-market, NY RTH).
Only reversal predictions during NY RTH are flagged as executable for paper
trading (Phase 4).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
logger = logging.getLogger(__name__)

import numpy as np

from alpha_lab.dashboard.engine.models import ObservationStatus, ObservationWindow
from alpha_lab.dashboard.model import (
    CLASS_NAMES,
    FEATURE_COLUMNS,
    Prediction,
)
from alpha_lab.dashboard.model.model_manager import ModelManager


class PredictionEngine:
    """Runs CatBoost inference on observation window features."""

    def __init__(
        self,
        model_manager: ModelManager,
        min_confidence: float = 0.70,
    ) -> None:
        self._mm = model_manager
        self._min_confidence = min_confidence
        self._callbacks: list[Callable[[Prediction], None]] = []

    def predict(self, observation: ObservationWindow) -> Prediction | None:
        """Run inference on a completed observation.

        Returns None if no model is loaded or observation is not completed.
        """
        model = self._mm.model
        if model is None:
            logger.info("Prediction skipped: no model loaded")
            return None

        if observation.status != ObservationStatus.COMPLETED:
            return None
        if observation.features is None:
            logger.info("Prediction skipped: no features computed for event=%s", observation.event.event_id[:8])
            return None

        # Extract feature vector using the model's own feature names
        # (supports both 3-feature and 6-11 feature models)
        model_feature_names = getattr(model, "feature_names_", None)
        if model_feature_names is not None:
            feature_names = list(model_feature_names)
            # CatBoost assigns numeric names when trained without explicit names
            # In that case, use the first N features from FEATURE_COLUMNS
            if all(f.isdigit() for f in feature_names):
                feature_names = list(FEATURE_COLUMNS[:len(feature_names)])
        else:
            feature_names = list(FEATURE_COLUMNS)

        # Build feature array, using NaN for any missing features
        feature_values = []
        for col in feature_names:
            feature_values.append(observation.features.get(col, float("nan")))
        features = np.array([feature_values])

        # Run inference
        predicted_idx = int(model.predict(features).flat[0])
        proba = model.predict_proba(features)[0]

        predicted_class = CLASS_NAMES[predicted_idx]
        probabilities = {CLASS_NAMES[i]: float(p) for i, p in enumerate(proba)}
        reversal_prob = probabilities.get("tradeable_reversal", 0.0)

        # Execution eligibility: reversal + NY RTH + confidence threshold
        is_executable = (
            predicted_class == "tradeable_reversal"
            and observation.event.session == "ny_rth"
            and reversal_prob >= self._min_confidence
        )

        logger.info(
            "Prediction: class=%s, is_executable=%s, session=%s, direction=%s, "
            "probabilities={%s}, features={%s}",
            predicted_class, is_executable, observation.event.session,
            observation.event.trade_direction.value,
            ", ".join(f"{k}: {v:.3f}" for k, v in probabilities.items()),
            ", ".join(f"{k}: {v:.4f}" for k, v in observation.features.items()),
        )

        active_version = self._mm.get_active_version()
        model_version = active_version["version"] if active_version else "unknown"

        prediction = Prediction(
            event_id=observation.event.event_id,
            timestamp=observation.event.timestamp,
            observation=observation,
            predicted_class=predicted_class,
            probabilities=probabilities,
            features=dict(observation.features),
            is_executable=is_executable,
            trade_direction=observation.event.trade_direction,
            level_price=observation.event.level_zone.representative_price,
            model_version=model_version,
        )

        for cb in self._callbacks:
            cb(prediction)

        return prediction

    def on_prediction(self, callback: Callable[[Prediction], None]) -> None:
        """Register callback for new predictions."""
        self._callbacks.append(callback)
