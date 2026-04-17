"""
Phase 3 data models for model inference and outcome tracking.

Defines the Prediction and ResolvedOutcome dataclasses, plus constants
for the 3-class CatBoost model used across model_manager, prediction_engine,
and outcome_tracker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from alpha_lab.dashboard.engine.models import ObservationWindow, TradeDirection

# 3-class CatBoost model mapping
CLASS_NAMES = {
    0: "tradeable_reversal",
    1: "trap_reversal",
    2: "aggressive_blowthrough",
}
CLASS_INDEX = {v: k for k, v in CLASS_NAMES.items()}

# All features the dashboard can compute at runtime (MBP-1 compatible).
# The active model may use a subset (RFECV-selected).
FEATURE_COLUMNS = [
    # Interaction features (post-touch observation window)
    "int_time_beyond_level",
    "int_time_within_2pts",
    "int_absorption_ratio",
    # Approach features (pre-touch order flow window)
    "app_large_trade_vol_pct",
    "app_trade_count",
    "app_volume_acceleration",
    "app_avg_trade_size",
    "app_avg_tob_imbalance",
    "app_max_spread",
    "app_volatility_recent",
    "app_volatility_ratio",
]


@dataclass
class Prediction:
    """Output of CatBoost inference on a completed observation window."""

    event_id: str
    timestamp: datetime
    observation: ObservationWindow
    predicted_class: str  # One of CLASS_NAMES values
    probabilities: dict[str, float]  # {class_name: probability}
    features: dict[str, float]  # The 3 input features
    is_executable: bool  # True only if reversal AND during NY RTH
    trade_direction: TradeDirection
    level_price: Decimal
    model_version: str


@dataclass
class ResolvedOutcome:
    """Result of outcome tracking after a prediction."""

    event_id: str
    prediction: Prediction
    mfe_points: float  # Maximum favorable excursion
    mae_points: float  # Maximum adverse excursion
    resolution_type: str  # 'tp_hit', 'sl_hit', 'session_end', 'flatten'
    prediction_correct: bool
    actual_class: str  # What actually happened
    resolved_at: datetime
