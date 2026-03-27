"""
Shared test fixtures for Phase 3 — Model Inference & Signal Generation.

Provides a synthetic CatBoost 3-class model trained on separable data,
plus helper functions for creating test observations and predictions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_lab.dashboard.engine.models import (
    LevelSide,
    LevelZone,
    ObservationStatus,
    ObservationWindow,
    TouchEvent,
    TradeDirection,
)
from alpha_lab.dashboard.model import Prediction
from alpha_lab.dashboard.pipeline.rithmic_client import TradeUpdate

BASE_TS = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)


@pytest.fixture
def catboost_model_path(tmp_path: Path) -> Path:
    """Train and save a minimal 3-class CatBoost model for testing.

    The model maps 3 float features to 3 classes:
      0 = tradeable_reversal
      1 = trap_reversal
      2 = aggressive_blowthrough

    Synthetic data is clearly separable:
    - Class 0 (reversal): high absorption, low time_beyond, high time_within
    - Class 1 (trap): medium values
    - Class 2 (blowthrough): low absorption, high time_beyond, low time_within
    """
    from catboost import CatBoostClassifier

    rng = np.random.default_rng(42)
    n = 100

    x0 = np.column_stack([
        rng.uniform(0, 30, n),
        rng.uniform(200, 300, n),
        rng.uniform(0.7, 1.0, n),
    ])
    x1 = np.column_stack([
        rng.uniform(60, 150, n),
        rng.uniform(100, 200, n),
        rng.uniform(0.3, 0.6, n),
    ])
    x2 = np.column_stack([
        rng.uniform(180, 300, n),
        rng.uniform(0, 80, n),
        rng.uniform(0.0, 0.2, n),
    ])

    x = np.vstack([x0, x1, x2])
    y = np.array([0] * n + [1] * n + [2] * n)

    model = CatBoostClassifier(
        iterations=50,
        depth=3,
        learning_rate=0.1,
        loss_function="MultiClass",
        verbose=0,
        random_seed=42,
        allow_writing_files=False,
    )
    model.fit(x, y)

    model_path = tmp_path / "test_model_v1.cbm"
    model.save_model(str(model_path))
    return model_path


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """Empty model directory for ModelManager tests."""
    d = tmp_path / "models"
    d.mkdir()
    return d


def make_observation(
    session: str = "ny_rth",
    direction: TradeDirection = TradeDirection.LONG,
    level_price: float = 20100.00,
    features: dict | None = None,
    event_id: str = "test_event_1",
    status: ObservationStatus = ObservationStatus.COMPLETED,
) -> ObservationWindow:
    """Create an ObservationWindow for testing."""
    zone = LevelZone(
        zone_id="test_zone",
        representative_price=Decimal(str(level_price)),
        side=LevelSide.LOW if direction == TradeDirection.LONG else LevelSide.HIGH,
    )
    event = TouchEvent(
        event_id=event_id,
        timestamp=BASE_TS,
        level_zone=zone,
        trade_direction=direction,
        price_at_touch=Decimal(str(level_price)),
        session=session,
    )
    if features is None:
        features = {
            "int_time_beyond_level": 15.0,
            "int_time_within_2pts": 250.0,
            "int_absorption_ratio": 0.85,
        }
    return ObservationWindow(
        event=event,
        start_time=BASE_TS,
        end_time=BASE_TS + timedelta(minutes=5),
        status=status,
        features=features,
    )


def make_prediction(
    predicted_class: str = "tradeable_reversal",
    direction: TradeDirection = TradeDirection.LONG,
    level_price: float = 20100.00,
    session: str = "ny_rth",
    event_id: str = "test_event_1",
) -> Prediction:
    """Create a Prediction for outcome tracker tests."""
    obs = make_observation(
        session=session,
        direction=direction,
        level_price=level_price,
        event_id=event_id,
    )
    return Prediction(
        event_id=event_id,
        timestamp=BASE_TS,
        observation=obs,
        predicted_class=predicted_class,
        probabilities={
            "tradeable_reversal": 0.7,
            "trap_reversal": 0.2,
            "aggressive_blowthrough": 0.1,
        },
        features=obs.features,
        is_executable=(predicted_class == "tradeable_reversal" and session == "ny_rth"),
        trade_direction=direction,
        level_price=Decimal(str(level_price)),
        model_version="test_v1",
    )


def make_trade(
    ts_offset_s: float = 0,
    price: float = 20100.00,
    size: int = 5,
) -> TradeUpdate:
    """Create a TradeUpdate at BASE_TS + offset."""
    ts = BASE_TS + timedelta(seconds=ts_offset_s)
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side="BUY",
        symbol="NQH6",
    )
