"""
Phase 3 — Prediction Engine Tests

Tests CatBoost inference on observation window features. The prediction
engine is the bridge between feature computation and trading decisions.

Business context: When the observation window completes, the model has
~0ms to produce a prediction (no latency requirement, but should be
near-instant). The prediction determines whether paper trades fire on
all 5 simulated accounts. 86.1% reversal precision means roughly 1 in
7 reversal calls is wrong — the paper trading system must handle this.
"""

from __future__ import annotations

from pathlib import Path

from alpha_lab.dashboard.model import Prediction
from alpha_lab.dashboard.model.model_manager import ModelManager
from alpha_lab.dashboard.model.prediction_engine import PredictionEngine

from .conftest import make_observation

# ── Helpers ──────────────────────────────────────────────────────


def _setup_engine(
    model_dir: Path, catboost_model_path: Path,
) -> PredictionEngine:
    """Create a PredictionEngine with an active model."""
    mgr = ModelManager(model_dir)
    version = mgr.upload_model(catboost_model_path)
    mgr.activate_model(version["id"])
    return PredictionEngine(mgr)


# ── Tests ────────────────────────────────────────────────────────


def test_predict_returns_prediction(
    model_dir: Path, catboost_model_path: Path,
):
    """Valid observation with loaded model produces a Prediction."""
    engine = _setup_engine(model_dir, catboost_model_path)
    obs = make_observation()

    pred = engine.predict(obs)

    assert pred is not None
    assert isinstance(pred, Prediction)
    assert pred.event_id == obs.event.event_id


def test_predict_no_model_returns_none(model_dir: Path):
    """No active model returns None, not an error."""
    mgr = ModelManager(model_dir)
    engine = PredictionEngine(mgr)
    obs = make_observation()

    pred = engine.predict(obs)

    assert pred is None


def test_prediction_class_is_valid(
    model_dir: Path, catboost_model_path: Path,
):
    """Predicted class is one of the 3 valid class strings."""
    engine = _setup_engine(model_dir, catboost_model_path)
    obs = make_observation()

    pred = engine.predict(obs)

    assert pred.predicted_class in {
        "tradeable_reversal",
        "trap_reversal",
        "aggressive_blowthrough",
    }


def test_probabilities_sum_to_one(
    model_dir: Path, catboost_model_path: Path,
):
    """Class probabilities sum to approximately 1.0."""
    engine = _setup_engine(model_dir, catboost_model_path)
    obs = make_observation()

    pred = engine.predict(obs)

    assert abs(sum(pred.probabilities.values()) - 1.0) < 0.001
    assert len(pred.probabilities) == 3
    assert all(
        k in pred.probabilities
        for k in ["tradeable_reversal", "trap_reversal", "aggressive_blowthrough"]
    )


def test_features_passed_correctly(
    model_dir: Path, catboost_model_path: Path,
):
    """The 3 features from the observation appear in the Prediction."""
    engine = _setup_engine(model_dir, catboost_model_path)
    features = {
        "int_time_beyond_level": 42.0,
        "int_time_within_2pts": 180.0,
        "int_absorption_ratio": 0.65,
    }
    obs = make_observation(features=features)

    pred = engine.predict(obs)

    assert pred.features == features


def test_executable_reversal_during_rth(
    model_dir: Path, catboost_model_path: Path,
):
    """is_executable follows the rule: reversal AND ny_rth."""
    engine = _setup_engine(model_dir, catboost_model_path)
    obs = make_observation(session="ny_rth")

    pred = engine.predict(obs)

    # Verify the logical rule regardless of predicted class
    expected = (
        pred.predicted_class == "tradeable_reversal"
        and obs.event.session == "ny_rth"
    )
    assert pred.is_executable == expected


def test_not_executable_outside_rth(
    model_dir: Path, catboost_model_path: Path,
):
    """Prediction outside NY RTH is never executable."""
    engine = _setup_engine(model_dir, catboost_model_path)
    obs = make_observation(session="london")

    pred = engine.predict(obs)

    # Regardless of predicted class, non-RTH sessions are never executable
    assert pred.is_executable is False


def test_not_executable_non_reversal(
    model_dir: Path, catboost_model_path: Path,
):
    """Non-reversal prediction during RTH is not executable."""
    engine = _setup_engine(model_dir, catboost_model_path)
    # Use features that strongly suggest trap (medium values)
    obs = make_observation(
        session="ny_rth",
        features={
            "int_time_beyond_level": 100.0,
            "int_time_within_2pts": 150.0,
            "int_absorption_ratio": 0.45,
        },
    )

    pred = engine.predict(obs)

    # Verify rule: only reversals during RTH are executable
    expected = pred.predicted_class == "tradeable_reversal"
    assert pred.is_executable == expected


def test_not_executable_blowthrough(
    model_dir: Path, catboost_model_path: Path,
):
    """Blowthrough-favoring features produce correct is_executable flag."""
    engine = _setup_engine(model_dir, catboost_model_path)
    obs = make_observation(
        session="ny_rth",
        features={
            "int_time_beyond_level": 250.0,
            "int_time_within_2pts": 30.0,
            "int_absorption_ratio": 0.05,
        },
    )

    pred = engine.predict(obs)

    # Whatever class the model predicts, the rule must hold
    expected = pred.predicted_class == "tradeable_reversal"
    assert pred.is_executable == expected


def test_callback_fires_on_prediction(
    model_dir: Path, catboost_model_path: Path,
):
    """Registered callback receives the Prediction."""
    engine = _setup_engine(model_dir, catboost_model_path)
    received: list[Prediction] = []
    engine.on_prediction(lambda p: received.append(p))

    obs = make_observation()
    pred = engine.predict(obs)

    assert len(received) == 1
    assert received[0] is pred
    assert received[0].event_id == obs.event.event_id
