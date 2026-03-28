# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from run_prediction_analytics import (
    PredictionRow,
    build_confusion_and_metrics,
    build_detailed_rows_for_modes,
    compute_entry_fallback_stats,
    confidence_bucket,
    evaluate_traded_outcome,
    get_detailed_fieldnames,
    resolve_actual_class,
)

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


def test_both_mode_detailed_rows_include_resolution_mode() -> None:
    mode_rows = {
        "optimistic": [
            {"timestamp": "2026-01-01T15:30:00+00:00", "event_id": "e1", "value": 1}
        ],
        "pessimistic": [
            {"timestamp": "2026-01-01T15:30:00+00:00", "event_id": "e1", "value": 2}
        ],
    }
    rows = build_detailed_rows_for_modes(mode_rows)
    assert len(rows) == 2
    assert {r["resolution_mode"] for r in rows} == {"optimistic", "pessimistic"}


def test_entry_price_fallback_count_and_rate() -> None:
    rows = [
        {"entry_price_is_fallback": True},
        {"entry_price_is_fallback": False},
        {"entry_price_is_fallback": True},
    ]
    count, rate = compute_entry_fallback_stats(rows)
    assert count == 2
    assert rate == 2 / 3


def test_is_executable_field_present_in_prediction_row_dataclass() -> None:
    fields = PredictionRow.__dataclass_fields__
    assert "is_executable" in fields


def test_detailed_fieldnames_cover_emitted_row_keys() -> None:
    emitted_rows = build_detailed_rows_for_modes(
        {
            "optimistic": [
                {
                    "event_id": "e1",
                    "timestamp": "2026-01-01T15:30:00+00:00",
                    "session": "ny_rth",
                    "trade_direction": "long",
                    "predicted_class": "tradeable_reversal",
                    "prob_tradeable_reversal": 0.8,
                    "prob_trap_reversal": 0.1,
                    "prob_aggressive_blowthrough": 0.1,
                    "predicted_confidence": 0.8,
                    "reversal_probability": 0.8,
                    "confidence_bucket": "[0.80,0.90)",
                    "level_type": "pdh",
                    "level_price": 20000.0,
                    "model_version": "v1",
                    "feature_int_time_beyond_level": 1.0,
                    "feature_int_time_within_2pts": 2.0,
                    "feature_int_absorption_ratio": 0.5,
                    "entry_price_at_prediction": 20001.0,
                    "entry_price_is_fallback": False,
                    "is_executable": True,
                    "mfe_points": 20.0,
                    "mae_points": 5.0,
                    "default_actual_class": "tradeable_reversal",
                    "default_resolution_type": "tp_hit",
                    "actual_class": "tradeable_reversal",
                    "prediction_correct": True,
                    "tp15_sl15_exit_reason": "tp",
                    "tp15_sl15_pnl_points": 15.0,
                    "tp15_sl25_exit_reason": "tp",
                    "tp15_sl25_pnl_points": 15.0,
                    "tp15_sl30_exit_reason": "tp",
                    "tp15_sl30_pnl_points": 15.0,
                }
            ]
        }
    )
    fieldnames = set(get_detailed_fieldnames()) | {"resolution_mode"}
    assert set(emitted_rows[0].keys()).issubset(fieldnames)
