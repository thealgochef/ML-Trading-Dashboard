from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from alpha_lab.dashboard.engine.feature_computer import FeatureComputer
from alpha_lab.dashboard.engine.models import (
    KeyLevel,
    LevelSide,
    LevelType,
    LevelZone,
    TouchEvent,
    TradeDirection,
)
from alpha_lab.dashboard.engine.observation_manager import ObservationManager
from alpha_lab.dashboard.trading import TRAILING_DD
from run_prediction_analytics import (
    build_confusion_and_metrics,
    confidence_bucket,
    evaluate_traded_outcome,
    resolve_actual_class,
)


def test_confidence_bucket_assignment_boundaries() -> None:
    assert confidence_bucket(0.00) == "[0.00,0.50)"
    assert confidence_bucket(0.50) == "[0.50,0.60)"
    assert confidence_bucket(0.8999) == "[0.80,0.90)"
    assert confidence_bucket(0.90) == "[0.90,1.00)"
    assert confidence_bucket(1.0) == "[0.90,1.00)"


def test_confusion_matrix_and_per_class_metrics() -> None:
    rows = [
        {"actual_class": "tradeable_reversal", "predicted_class": "tradeable_reversal"},
        {"actual_class": "tradeable_reversal", "predicted_class": "trap_reversal"},
        {"actual_class": "trap_reversal", "predicted_class": "tradeable_reversal"},
        {"actual_class": "aggressive_blowthrough", "predicted_class": "aggressive_blowthrough"},
    ]
    conf, metrics, _ = build_confusion_and_metrics(rows)

    assert conf[("tradeable_reversal", "tradeable_reversal")] == 1
    assert conf[("tradeable_reversal", "trap_reversal")] == 1
    assert conf[("trap_reversal", "tradeable_reversal")] == 1
    assert metrics["tradeable_reversal"]["precision"] == 0.5
    assert metrics["tradeable_reversal"]["recall"] == 0.5
    assert metrics["__overall__"]["accuracy_like"] == 0.5


def test_optimistic_vs_pessimistic_tie_handling_differs_only_on_tie() -> None:
    optimistic = resolve_actual_class(mfe_points=30.0, mae_points=40.0, mode="optimistic")
    pessimistic = resolve_actual_class(mfe_points=30.0, mae_points=40.0, mode="pessimistic")
    assert optimistic == "tradeable_reversal"
    assert pessimistic == "trap_reversal"

    assert resolve_actual_class(30.0, 10.0, mode="optimistic") == "tradeable_reversal"
    assert resolve_actual_class(30.0, 10.0, mode="pessimistic") == "tradeable_reversal"


def test_traded_outcome_reresolution_for_15_15_15_25_15_30() -> None:
    direction = "long"
    entry = 100.0

    path_tp = [101.0, 103.0, 115.0]
    assert evaluate_traded_outcome(direction, entry, path_tp, 15, 15)[0] == "tp"
    assert evaluate_traded_outcome(direction, entry, path_tp, 15, 25)[0] == "tp"
    assert evaluate_traded_outcome(direction, entry, path_tp, 15, 30)[0] == "tp"

    path_split = [99.0, 84.0, 100.0, 115.0]
    assert evaluate_traded_outcome(direction, entry, path_split, 15, 15)[0] == "sl"
    assert evaluate_traded_outcome(direction, entry, path_split, 15, 25)[0] == "tp"
    assert evaluate_traded_outcome(direction, entry, path_split, 15, 30)[0] == "tp"


def test_observation_rejection_instrumentation_is_additive_behavior_unchanged() -> None:
    mgr = ObservationManager(FeatureComputer())

    zone = LevelZone(
        zone_id="z1",
        representative_price=Decimal("100.0"),
        side=LevelSide.LOW,
        levels=[
            KeyLevel(
                level_type=LevelType.PDL,
                price=Decimal("100.0"),
                side=LevelSide.LOW,
                available_from=datetime(2026, 1, 2, 14, 30, tzinfo=UTC),
                source_session_date=date(2026, 1, 2),
            )
        ],
    )
    e1 = TouchEvent(event_id="e1", level_zone=zone, trade_direction=TradeDirection.LONG, session="ny_rth")
    e2 = TouchEvent(event_id="e2", level_zone=zone, trade_direction=TradeDirection.LONG, session="ny_rth")

    w1 = mgr.start_observation(e1)
    w2 = mgr.start_observation(e2)

    assert w1 is not None
    assert w2 is None
    stats = mgr.get_censoring_stats()
    assert stats["summary"]["accepted_touches"] == 1
    assert stats["summary"]["rejected_touches"] == 1
    assert stats["by_group"][0]["reason"] == "already_active"


def test_missing_optional_fields_do_not_break_metrics() -> None:
    rows = [
        {"actual_class": None, "predicted_class": "tradeable_reversal"},
        {"actual_class": "trap_reversal", "predicted_class": None},
    ]
    conf, metrics, _ = build_confusion_and_metrics(rows)
    assert sum(conf.values()) == 0
    assert metrics["__overall__"]["accuracy_like"] == 0.0


def test_payout_dd_constants_untouched() -> None:
    assert float(TRAILING_DD) == 2000.0
