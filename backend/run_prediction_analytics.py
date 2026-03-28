"""
Prediction analytics runner for dashboard replay data.

Builds a prediction-level dataset and summary analytics from the existing
pipeline components (touch -> observation -> prediction -> outcome tracking),
without changing production behavior.

Outputs:
1) detailed CSV (one row per prediction)
2) summary CSV (compact key metrics + grouped breakdowns)
3) terminal summary with confusion, class metrics, confidence buckets,
   session/level summaries, optimistic vs pessimistic deltas, and TP/SL notes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time as time_mod
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

# Ensure backend/src is on sys.path
_BACKEND_DIR = Path(__file__).resolve().parent
_SRC_DIR = _BACKEND_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from zoneinfo import ZoneInfo

from alpha_lab.dashboard.config.settings import DashboardSettings
from alpha_lab.dashboard.engine.feature_computer import FeatureComputer
from alpha_lab.dashboard.engine.level_engine import LevelEngine, _cme_day_start_utc
from alpha_lab.dashboard.engine.models import ObservationStatus, TradeDirection
from alpha_lab.dashboard.engine.observation_manager import ObservationManager
from alpha_lab.dashboard.engine.touch_detector import TouchDetector
from alpha_lab.dashboard.model import CLASS_NAMES
from alpha_lab.dashboard.model.model_manager import ModelManager
from alpha_lab.dashboard.model.outcome_tracker import MAE_STOP, MFE_TARGET, TRAP_MFE_MIN, OutcomeTracker
from alpha_lab.dashboard.model.prediction_engine import PredictionEngine
from alpha_lab.dashboard.pipeline.pipeline_service import PipelineService
from alpha_lab.dashboard.pipeline.replay_client import ReplayClient
from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, ConnectionStatus, TradeUpdate

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s - %(message)s")
logger = logging.getLogger("prediction_analytics")

ET = ZoneInfo("America/New_York")

ResolutionMode = Literal["optimistic", "pessimistic"]
TP_SL_GEOMETRIES: tuple[tuple[int, int], ...] = ((15, 15), (15, 25), (15, 30))

# Stable confidence buckets for deterministic diffs
_CONF_BUCKETS = [
    (0.00, 0.50),
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.80),
    (0.80, 0.90),
    (0.90, 1.01),
]


@dataclass
class PredictionRow:
    event_id: str
    timestamp: datetime
    session: str
    trade_direction: str
    predicted_class: str
    probabilities: dict[str, float]
    predicted_confidence: float
    reversal_probability: float
    confidence_bucket: str
    level_type: str | None
    level_price: float
    model_version: str | None
    feature_int_time_beyond_level: float | None
    feature_int_time_within_2pts: float | None
    feature_int_absorption_ratio: float | None
    entry_price_at_prediction: float
    mfe_points: float | None
    mae_points: float | None


def _auto_load_model(model_manager: ModelManager, model_dir: Path) -> None:
    cbm_files = sorted(model_dir.glob("*.cbm"))
    if not cbm_files:
        raise FileNotFoundError(f"No .cbm model in {model_dir}")

    preferred = model_dir / "dashboard_3feature_v1.cbm"
    chosen = preferred if preferred in cbm_files else cbm_files[0]

    version = model_manager.upload_model(chosen)
    model_manager.activate_model(version["id"])
    logger.info("Loaded model: %s", chosen.name)


def _cme_trading_date(ts_utc: datetime) -> date:
    ts_et = ts_utc.astimezone(ET)
    if ts_et.time() >= time(18, 0):
        return (ts_et + timedelta(days=1)).date()
    return ts_et.date()


def _session_end_for_prediction(ts_utc: datetime) -> datetime:
    """Return flatten cutoff for the prediction's NY date in UTC (3:55 PM ET)."""
    ts_et = ts_utc.astimezone(ET)
    end_et = datetime.combine(ts_et.date(), time(15, 55), tzinfo=ET)
    return end_et.astimezone(UTC)


def confidence_bucket(prob: float) -> str:
    p = max(0.0, min(1.0, float(prob)))
    for lo, hi in _CONF_BUCKETS:
        if lo <= p < hi:
            hi_label = 1.0 if hi > 1.0 else hi
            return f"[{lo:.2f},{hi_label:.2f})"
    return "[0.90,1.00)"


def resolve_actual_class(mfe_points: float, mae_points: float, mode: ResolutionMode) -> str:
    """Resolve actual class from MFE/MAE with analytics-only tie-mode control."""
    mfe_hit = mfe_points >= MFE_TARGET
    mae_hit = mae_points >= MAE_STOP

    if mfe_hit and mae_hit:
        if mode == "optimistic":
            return "tradeable_reversal"
        return "trap_reversal" if mfe_points >= TRAP_MFE_MIN else "aggressive_blowthrough"

    if mfe_hit:
        return "tradeable_reversal"

    if mae_hit:
        return "trap_reversal" if mfe_points >= TRAP_MFE_MIN else "aggressive_blowthrough"

    if mfe_points >= TRAP_MFE_MIN:
        return "trap_reversal"
    return "aggressive_blowthrough"


def evaluate_traded_outcome(
    direction: str,
    entry_price: float,
    trade_path_prices: list[float],
    tp_points: int,
    sl_points: int,
) -> tuple[str, float]:
    """Evaluate TP/SL outcome using existing PositionMonitor semantics.

    Returns: (exit_reason, pnl_points)
    exit_reason in {'tp', 'sl', 'session_end'}.
    """
    if direction == "long":
        tp_price = entry_price + tp_points
        sl_price = entry_price - sl_points
        for px in trade_path_prices:
            if px >= tp_price:
                return "tp", float(tp_points)
            if px <= sl_price:
                return "sl", float(-sl_points)
    else:
        tp_price = entry_price - tp_points
        sl_price = entry_price + sl_points
        for px in trade_path_prices:
            if px <= tp_price:
                return "tp", float(tp_points)
            if px >= sl_price:
                return "sl", float(-sl_points)

    # Flatten/session-end fallback at last observed price
    if not trade_path_prices:
        return "session_end", 0.0

    last_px = trade_path_prices[-1]
    if direction == "long":
        return "session_end", float(last_px - entry_price)
    return "session_end", float(entry_price - last_px)


def build_confusion_and_metrics(rows: list[dict]) -> tuple[dict[tuple[str, str], int], dict[str, dict[str, float]], dict[str, int]]:
    labels = [CLASS_NAMES[i] for i in sorted(CLASS_NAMES.keys())]
    conf: dict[tuple[str, str], int] = {(a, b): 0 for a in labels for b in labels}

    for r in rows:
        actual = r.get("actual_class")
        pred = r.get("predicted_class")
        if actual in labels and pred in labels:
            conf[(actual, pred)] += 1

    support: dict[str, int] = {c: sum(conf[(c, p)] for p in labels) for c in labels}
    total = sum(support.values())
    correct = sum(conf[(c, c)] for c in labels)
    overall_accuracy = (correct / total) if total else 0.0

    metrics: dict[str, dict[str, float]] = {}
    for cls in labels:
        tp = conf[(cls, cls)]
        fp = sum(conf[(a, cls)] for a in labels if a != cls)
        fn = sum(conf[(cls, p)] for p in labels if p != cls)
        tn = total - tp - fp - fn
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        class_acc = (tp + tn) / total if total else 0.0
        metrics[cls] = {
            "support": float(support[cls]),
            "precision": precision,
            "recall": recall,
            "accuracy_like": class_acc,
            "tp": float(tp),
            "fp": float(fp),
            "fn": float(fn),
        }

    metrics["__overall__"] = {
        "support": float(total),
        "precision": 0.0,
        "recall": 0.0,
        "accuracy_like": overall_accuracy,
        "tp": float(correct),
        "fp": 0.0,
        "fn": 0.0,
    }

    return conf, metrics, support


def bucket_summary(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["confidence_bucket"]].append(r)

    out = []
    for b in sorted(grouped.keys()):
        grp = grouped[b]
        n = len(grp)
        avg_conf = sum(r["reversal_probability"] for r in grp) / n if n else 0.0
        realized_rev = sum(1 for r in grp if r.get("actual_class") == "tradeable_reversal") / n if n else 0.0

        def _wr(tp_col: str, sl_col: str) -> float:
            wins = sum(1 for r in grp if r.get(tp_col) == "tp")
            losses = sum(1 for r in grp if r.get(tp_col) == "sl")
            denom = wins + losses
            return wins / denom if denom else 0.0

        ev_15_15 = sum(r.get("tp15_sl15_pnl_points", 0.0) for r in grp) / n if n else 0.0
        ev_15_25 = sum(r.get("tp15_sl25_pnl_points", 0.0) for r in grp) / n if n else 0.0
        ev_15_30 = sum(r.get("tp15_sl30_pnl_points", 0.0) for r in grp) / n if n else 0.0

        out.append({
            "confidence_bucket": b,
            "count": n,
            "avg_reversal_probability": round(avg_conf, 6),
            "empirical_reversal_frequency": round(realized_rev, 6),
            "winrate_tp15_sl15": round(_wr("tp15_sl15_exit_reason", "tp15_sl15_exit_reason"), 6),
            "winrate_tp15_sl25": round(_wr("tp15_sl25_exit_reason", "tp15_sl25_exit_reason"), 6),
            "winrate_tp15_sl30": round(_wr("tp15_sl30_exit_reason", "tp15_sl30_exit_reason"), 6),
            "expectancy_points_tp15_sl15": round(ev_15_15, 6),
            "expectancy_points_tp15_sl25": round(ev_15_25, 6),
            "expectancy_points_tp15_sl30": round(ev_15_30, 6),
        })

    return out


def breakdown_summary(rows: list[dict], by_keys: tuple[str, ...], min_samples: int) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[tuple(r.get(k) for k in by_keys)].append(r)

    out: list[dict] = []
    for key in sorted(grouped.keys()):
        grp = grouped[key]
        if len(grp) < min_samples:
            continue

        wins_1530 = sum(1 for r in grp if r.get("tp15_sl30_exit_reason") == "tp")
        losses_1530 = sum(1 for r in grp if r.get("tp15_sl30_exit_reason") == "sl")
        denom_1530 = wins_1530 + losses_1530

        class_mix = defaultdict(int)
        for r in grp:
            class_mix[r["predicted_class"]] += 1

        row = {
            "group_key": "|".join(str(v) for v in key),
            "count": len(grp),
            "winrate_tp15_sl30": round(wins_1530 / denom_1530, 6) if denom_1530 else 0.0,
            "expectancy_points_tp15_sl15": round(sum(r.get("tp15_sl15_pnl_points", 0.0) for r in grp) / len(grp), 6),
            "expectancy_points_tp15_sl25": round(sum(r.get("tp15_sl25_pnl_points", 0.0) for r in grp) / len(grp), 6),
            "expectancy_points_tp15_sl30": round(sum(r.get("tp15_sl30_pnl_points", 0.0) for r in grp) / len(grp), 6),
            "class_mix_json": json.dumps(dict(sorted(class_mix.items())), sort_keys=True),
        }
        for idx, k in enumerate(by_keys):
            row[k] = key[idx]
        out.append(row)

    return out


def mfe_mae_summary(rows: list[dict], key: str | None = None, min_samples: int = 1) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    if key is None:
        grouped["all"] = rows
    else:
        for r in rows:
            grouped[str(r.get(key))].append(r)

    out: list[dict] = []
    for grp_key in sorted(grouped.keys()):
        grp = [r for r in grouped[grp_key] if r.get("mfe_points") is not None and r.get("mae_points") is not None]
        if len(grp) < min_samples:
            continue
        mfe_vals = [float(r["mfe_points"]) for r in grp]
        mae_vals = [float(r["mae_points"]) for r in grp]
        out.append({
            "group": grp_key,
            "count": len(grp),
            "mfe_mean": round(sum(mfe_vals) / len(mfe_vals), 6),
            "mfe_max": round(max(mfe_vals), 6),
            "mae_mean": round(sum(mae_vals) / len(mae_vals), 6),
            "mae_max": round(max(mae_vals), 6),
        })
    return out


def _rows_for_mode(pred_rows: list[dict], mode: ResolutionMode) -> list[dict]:
    out = []
    for r in pred_rows:
        row = dict(r)
        mfe = row.get("mfe_points")
        mae = row.get("mae_points")
        if mfe is not None and mae is not None:
            row["actual_class"] = resolve_actual_class(float(mfe), float(mae), mode)
            row["prediction_correct"] = row["actual_class"] == row["predicted_class"]
        else:
            row["actual_class"] = None
            row["prediction_correct"] = None
        out.append(row)
    return out


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def run_prediction_analytics(
    data_dir: Path,
    model_dir: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    resolution_mode: Literal["optimistic", "pessimistic", "both"] = "both",
    min_breakdown_samples: int = 10,
) -> tuple[Path, Path]:
    """Run replay and generate prediction analytics exports."""
    logging.getLogger("alpha_lab").setLevel(logging.WARNING)

    settings = DashboardSettings()
    model_manager = ModelManager(model_dir)
    _auto_load_model(model_manager, model_dir)

    client = ReplayClient(data_dir=data_dir, start_date=start_date, end_date=end_date, speed=9999.0)
    pipeline = PipelineService(settings, client=client)
    level_engine = LevelEngine(pipeline._buffer)
    touch_detector = TouchDetector(level_engine)
    observation_manager = ObservationManager(FeatureComputer())
    prediction_engine = PredictionEngine(model_manager)
    outcome_tracker = OutcomeTracker()

    state: dict = {
        "latest_price": None,
        "predictions": [],
        "outcomes": {},
        "trade_ticks": [],
        "accepted_touches": [],
    }

    # Touch -> Observation
    def _on_touch(event):
        window = observation_manager.start_observation(event)
        if window is not None:
            level_type = None
            if event.level_zone.levels:
                level_type = event.level_zone.levels[0].level_type.value
            state["accepted_touches"].append({
                "event_id": event.event_id,
                "timestamp": event.timestamp.isoformat(),
                "session": event.session,
                "level_type": level_type,
                "direction": event.trade_direction.value,
                "reason": "accepted",
            })

    touch_detector.on_touch(_on_touch)

    # Observation complete -> Prediction
    def _on_observation_complete(window):
        if window.status != ObservationStatus.COMPLETED:
            return
        prediction_engine.predict(window)

    observation_manager.on_observation_complete(_on_observation_complete)

    # Prediction -> record + start outcome tracking
    def _on_prediction(prediction):
        level_type = None
        zone = prediction.observation.event.level_zone
        if zone.levels:
            level_type = zone.levels[0].level_type.value

        entry_price = (
            float(state["latest_price"]) if state["latest_price"] is not None else float(prediction.level_price)
        )

        row = PredictionRow(
            event_id=prediction.event_id,
            timestamp=prediction.timestamp,
            session=prediction.observation.event.session,
            trade_direction=prediction.trade_direction.value,
            predicted_class=prediction.predicted_class,
            probabilities=dict(prediction.probabilities),
            predicted_confidence=max(prediction.probabilities.values()) if prediction.probabilities else 0.0,
            reversal_probability=float(prediction.probabilities.get("tradeable_reversal", 0.0)),
            confidence_bucket=confidence_bucket(float(prediction.probabilities.get("tradeable_reversal", 0.0))),
            level_type=level_type,
            level_price=float(prediction.level_price),
            model_version=prediction.model_version,
            feature_int_time_beyond_level=prediction.features.get("int_time_beyond_level"),
            feature_int_time_within_2pts=prediction.features.get("int_time_within_2pts"),
            feature_int_absorption_ratio=prediction.features.get("int_absorption_ratio"),
            entry_price_at_prediction=entry_price,
            mfe_points=None,
            mae_points=None,
        )
        state["predictions"].append(row)
        outcome_tracker.start_tracking(prediction)

    prediction_engine.on_prediction(_on_prediction)

    # Outcome resolved -> attach mfe/mae
    def _on_outcome_resolved(outcome):
        state["outcomes"][outcome.event_id] = {
            "mfe_points": outcome.mfe_points,
            "mae_points": outcome.mae_points,
            "default_actual_class": outcome.actual_class,
            "resolution_type": outcome.resolution_type,
        }

    outcome_tracker.on_outcome_resolved(_on_outcome_resolved)

    _last_reset_trading_date: date | None = None

    def _on_session_change(old_session, new_session, timestamp):
        nonlocal _last_reset_trading_date
        trading_date = _cme_trading_date(timestamp)

        is_cme_day_boundary = (old_session == "post_market" and new_session == "asia")
        is_bootstrap = (_last_reset_trading_date is None)

        if is_bootstrap:
            _last_reset_trading_date = trading_date
            level_engine.reset_daily()
            level_engine.compute_levels(trading_date, current_time=timestamp)
            return

        if is_cme_day_boundary and trading_date != _last_reset_trading_date:
            _last_reset_trading_date = trading_date
            outcome_tracker.on_session_end(timestamp)
            level_engine.reset_daily()
            level_engine.compute_levels(trading_date, current_time=timestamp)
            pipeline._buffer.evict()
            return

        level_engine.compute_levels(trading_date, current_time=timestamp)

    touch_detector.on_session_change(_on_session_change)

    def _on_trade(trade: TradeUpdate):
        state["latest_price"] = float(trade.price)
        state["trade_ticks"].append((trade.timestamp, float(trade.price)))
        touch_detector.on_trade(trade)
        observation_manager.on_trade(trade)
        outcome_tracker.on_trade(trade)

    def _on_bbo(bbo: BBOUpdate):
        observation_manager.on_bbo(bbo)

    def _on_connection_status(status: ConnectionStatus):
        observation_manager.on_connection_status(status)

    pipeline.register_trade_handler(_on_trade)
    pipeline.register_bbo_handler(_on_bbo)
    pipeline.register_connection_handler(_on_connection_status)

    def _on_file_loaded(date_str: str):
        if not client._preloading:
            return
        trading_date = date.fromisoformat(date_str)
        level_engine.reset_daily()
        day_start_utc = _cme_day_start_utc(trading_date)
        level_engine.compute_levels(trading_date, current_time=day_start_utc)

    client.on_file_loaded(_on_file_loaded)

    async def _run():
        await pipeline.start()
        client._step_mode = False
        client._pause_event.set()
        while client._thread is not None and client._thread.is_alive():
            await asyncio.sleep(0.1)

    t0 = time_mod.time()
    asyncio.run(_run())
    elapsed = time_mod.time() - t0

    # Ensure unresolved trackers close on final timestamp
    if state["trade_ticks"]:
        outcome_tracker.on_session_end(state["trade_ticks"][-1][0])

    # Build per-prediction rows
    pred_rows: list[dict] = []
    sorted_predictions = sorted(state["predictions"], key=lambda p: (p.timestamp, p.event_id))

    ticks = state["trade_ticks"]
    for pred in sorted_predictions:
        out = state["outcomes"].get(pred.event_id, {})
        mfe = out.get("mfe_points")
        mae = out.get("mae_points")

        # tick path from prediction timestamp to session-end for traded-outcome simulation
        end_ts = _session_end_for_prediction(pred.timestamp)
        path = [px for ts, px in ticks if pred.timestamp <= ts <= end_ts]

        row = {
            "event_id": pred.event_id,
            "timestamp": pred.timestamp.isoformat(),
            "session": pred.session,
            "trade_direction": pred.trade_direction,
            "predicted_class": pred.predicted_class,
            "prob_tradeable_reversal": round(pred.probabilities.get("tradeable_reversal", 0.0), 8),
            "prob_trap_reversal": round(pred.probabilities.get("trap_reversal", 0.0), 8),
            "prob_aggressive_blowthrough": round(pred.probabilities.get("aggressive_blowthrough", 0.0), 8),
            "predicted_confidence": round(pred.predicted_confidence, 8),
            "reversal_probability": round(pred.reversal_probability, 8),
            "confidence_bucket": pred.confidence_bucket,
            "level_type": pred.level_type,
            "level_price": pred.level_price,
            "model_version": pred.model_version,
            "feature_int_time_beyond_level": pred.feature_int_time_beyond_level,
            "feature_int_time_within_2pts": pred.feature_int_time_within_2pts,
            "feature_int_absorption_ratio": pred.feature_int_absorption_ratio,
            "entry_price_at_prediction": pred.entry_price_at_prediction,
            "mfe_points": mfe,
            "mae_points": mae,
            "default_actual_class": out.get("default_actual_class"),
            "default_resolution_type": out.get("resolution_type"),
        }

        for tp, sl in TP_SL_GEOMETRIES:
            reason, pnl_pts = evaluate_traded_outcome(
                direction=pred.trade_direction,
                entry_price=pred.entry_price_at_prediction,
                trade_path_prices=path,
                tp_points=tp,
                sl_points=sl,
            )
            key_prefix = f"tp{tp}_sl{sl}"
            row[f"{key_prefix}_exit_reason"] = reason
            row[f"{key_prefix}_pnl_points"] = round(pnl_pts, 8)

        pred_rows.append(row)

    mode_list: list[ResolutionMode]
    if resolution_mode == "both":
        mode_list = ["optimistic", "pessimistic"]
    else:
        mode_list = [resolution_mode]

    mode_rows = {mode: _rows_for_mode(pred_rows, mode) for mode in mode_list}

    # summary rows
    summary_rows: list[dict] = []
    for mode in mode_list:
        rows = mode_rows[mode]
        conf, metrics, support = build_confusion_and_metrics(rows)

        labels = [CLASS_NAMES[i] for i in sorted(CLASS_NAMES.keys())]
        for actual in labels:
            for pred_lbl in labels:
                summary_rows.append({
                    "section": "confusion_matrix",
                    "mode": mode,
                    "group": f"actual={actual}|predicted={pred_lbl}",
                    "metric": "count",
                    "value": conf[(actual, pred_lbl)],
                })

        for cls in labels:
            m = metrics[cls]
            summary_rows.extend([
                {"section": "class_metrics", "mode": mode, "group": cls, "metric": "support", "value": int(m["support"])},
                {"section": "class_metrics", "mode": mode, "group": cls, "metric": "precision", "value": round(m["precision"], 8)},
                {"section": "class_metrics", "mode": mode, "group": cls, "metric": "recall", "value": round(m["recall"], 8)},
                {"section": "class_metrics", "mode": mode, "group": cls, "metric": "accuracy_like", "value": round(m["accuracy_like"], 8)},
            ])

        tr_m = metrics.get("tradeable_reversal", {})
        summary_rows.extend([
            {"section": "tradeable_reversal_errors", "mode": mode, "group": "tradeable_reversal", "metric": "false_positives", "value": int(tr_m.get("fp", 0.0))},
            {"section": "tradeable_reversal_errors", "mode": mode, "group": "tradeable_reversal", "metric": "false_negatives", "value": int(tr_m.get("fn", 0.0))},
        ])

        for b in bucket_summary(rows):
            for metric, value in b.items():
                if metric == "confidence_bucket":
                    continue
                summary_rows.append({
                    "section": "confidence_buckets",
                    "mode": mode,
                    "group": b["confidence_bucket"],
                    "metric": metric,
                    "value": value,
                })

        for row in breakdown_summary(rows, ("session",), min_samples=min_breakdown_samples):
            for metric, value in row.items():
                if metric in {"session", "group_key"}:
                    continue
                summary_rows.append({
                    "section": "session_breakdown",
                    "mode": mode,
                    "group": row["group_key"],
                    "metric": metric,
                    "value": value,
                })

        for row in breakdown_summary(rows, ("level_type",), min_samples=min_breakdown_samples):
            for metric, value in row.items():
                if metric in {"level_type", "group_key"}:
                    continue
                summary_rows.append({
                    "section": "level_breakdown",
                    "mode": mode,
                    "group": row["group_key"],
                    "metric": metric,
                    "value": value,
                })

        for row in breakdown_summary(rows, ("session", "level_type"), min_samples=min_breakdown_samples):
            for metric, value in row.items():
                if metric in {"session", "level_type", "group_key"}:
                    continue
                summary_rows.append({
                    "section": "session_level_breakdown",
                    "mode": mode,
                    "group": row["group_key"],
                    "metric": metric,
                    "value": value,
                })

        for row in mfe_mae_summary(rows, key=None, min_samples=1):
            for metric, value in row.items():
                if metric == "group":
                    continue
                summary_rows.append({"section": "mfe_mae_all", "mode": mode, "group": row["group"], "metric": metric, "value": value})

        for row in mfe_mae_summary(rows, key="predicted_class", min_samples=min_breakdown_samples):
            for metric, value in row.items():
                if metric == "group":
                    continue
                summary_rows.append({"section": "mfe_mae_by_predicted_class", "mode": mode, "group": row["group"], "metric": metric, "value": value})

        for row in mfe_mae_summary(rows, key="level_type", min_samples=min_breakdown_samples):
            for metric, value in row.items():
                if metric == "group":
                    continue
                summary_rows.append({"section": "mfe_mae_by_level_type", "mode": mode, "group": row["group"], "metric": metric, "value": value})

    # Observation censoring summary
    obs_stats = observation_manager.get_censoring_stats()
    for key, value in sorted(obs_stats["summary"].items()):
        summary_rows.append({
            "section": "observation_censoring_summary",
            "mode": "n/a",
            "group": "all",
            "metric": key,
            "value": value,
        })

    for detail in obs_stats["by_group"]:
        group_key = "|".join([
            f"reason={detail['reason']}",
            f"session={detail['session']}",
            f"level_type={detail['level_type']}",
            f"direction={detail['direction']}",
        ])
        summary_rows.append({
            "section": "observation_censoring_breakdown",
            "mode": "n/a",
            "group": group_key,
            "metric": "count",
            "value": detail["count"],
        })

    # Add explicit optimistic vs pessimistic delta rows when both are present
    if "optimistic" in mode_rows and "pessimistic" in mode_rows:
        opt_rows = mode_rows["optimistic"]
        pes_rows = mode_rows["pessimistic"]
        _, opt_metrics, _ = build_confusion_and_metrics(opt_rows)
        _, pes_metrics, _ = build_confusion_and_metrics(pes_rows)
        delta_acc = pes_metrics["__overall__"]["accuracy_like"] - opt_metrics["__overall__"]["accuracy_like"]
        summary_rows.append({
            "section": "mode_delta",
            "mode": "optimistic_vs_pessimistic",
            "group": "overall",
            "metric": "accuracy_like_delta_pess_minus_opt",
            "value": round(delta_acc, 8),
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    detailed_csv = output_dir / f"{ts}_prediction_analytics_detailed.csv"
    summary_csv = output_dir / f"{ts}_prediction_analytics_summary.csv"

    detailed_fieldnames = [
        "event_id", "timestamp", "session", "trade_direction", "predicted_class",
        "prob_tradeable_reversal", "prob_trap_reversal", "prob_aggressive_blowthrough",
        "predicted_confidence", "reversal_probability", "confidence_bucket",
        "level_type", "level_price", "model_version",
        "feature_int_time_beyond_level", "feature_int_time_within_2pts", "feature_int_absorption_ratio",
        "entry_price_at_prediction", "mfe_points", "mae_points",
        "default_actual_class", "default_resolution_type", "actual_class", "prediction_correct",
        "tp15_sl15_exit_reason", "tp15_sl15_pnl_points",
        "tp15_sl25_exit_reason", "tp15_sl25_pnl_points",
        "tp15_sl30_exit_reason", "tp15_sl30_pnl_points",
    ]

    # Write rows for first selected mode into detailed CSV with mode-specific actual_class/prediction_correct
    primary_mode = mode_list[0]
    _write_csv(detailed_csv, mode_rows[primary_mode], detailed_fieldnames)

    summary_fieldnames = ["section", "mode", "group", "metric", "value"]
    _write_csv(summary_csv, summary_rows, summary_fieldnames)

    # Terminal summary
    active_model = model_manager.get_active_version()
    model_version = active_model["version"] if active_model else "unknown"
    print("\n" + "=" * 88)
    print("PREDICTION ANALYTICS SUMMARY")
    print("=" * 88)
    print(f"Date range: {start_date} -> {end_date}")
    print(f"Resolution mode: {resolution_mode}")
    print(f"TP/SL geometries evaluated: {', '.join([f'{tp}/{sl}' for tp, sl in TP_SL_GEOMETRIES])}")
    print(f"Model version: {model_version}")
    print(f"Predictions: {len(pred_rows)}")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Detailed CSV: {detailed_csv}")
    print(f"Summary CSV: {summary_csv}")

    for mode in mode_list:
        rows = mode_rows[mode]
        conf, metrics, _ = build_confusion_and_metrics(rows)
        labels = [CLASS_NAMES[i] for i in sorted(CLASS_NAMES.keys())]
        print("\n" + "-" * 88)
        print(f"Confusion Matrix ({mode}) [actual x predicted]")
        print("-" * 88)
        print("actual\\pred".ljust(28) + " ".join(lbl.ljust(24) for lbl in labels))
        for actual in labels:
            vals = [str(conf[(actual, p)]).ljust(24) for p in labels]
            print(actual.ljust(28) + " ".join(vals))

        print("\nPer-class metrics:")
        for cls in labels:
            m = metrics[cls]
            print(
                f"  {cls}: support={int(m['support'])}, precision={m['precision']:.4f}, "
                f"recall={m['recall']:.4f}, accuracy_like={m['accuracy_like']:.4f}, "
                f"fp={int(m['fp'])}, fn={int(m['fn'])}"
            )
        print(f"Overall accuracy_like={metrics['__overall__']['accuracy_like']:.4f}")

        print("\nConfidence bucket summary (bucketed empirical calibration; not probabilistic calibration fitting):")
        for b in bucket_summary(rows):
            print(
                "  "
                f"{b['confidence_bucket']}: n={b['count']}, "
                f"avg_rev_prob={b['avg_reversal_probability']:.4f}, "
                f"empirical_rev_freq={b['empirical_reversal_frequency']:.4f}, "
                f"EV_pts(15/15)={b['expectancy_points_tp15_sl15']:.3f}, "
                f"EV_pts(15/25)={b['expectancy_points_tp15_sl25']:.3f}, "
                f"EV_pts(15/30)={b['expectancy_points_tp15_sl30']:.3f}"
            )

        print("\nSession profitability summary (min samples filter applied):")
        for row in breakdown_summary(rows, ("session",), min_samples=min_breakdown_samples):
            print(
                f"  {row['group_key']}: n={row['count']}, WR15/30={row['winrate_tp15_sl30']:.4f}, "
                f"EV15/15={row['expectancy_points_tp15_sl15']:.3f}, "
                f"EV15/25={row['expectancy_points_tp15_sl25']:.3f}, "
                f"EV15/30={row['expectancy_points_tp15_sl30']:.3f}"
            )

        print("\nLevel profitability summary (min samples filter applied):")
        for row in breakdown_summary(rows, ("level_type",), min_samples=min_breakdown_samples):
            print(
                f"  {row['group_key']}: n={row['count']}, WR15/30={row['winrate_tp15_sl30']:.4f}, "
                f"EV15/15={row['expectancy_points_tp15_sl15']:.3f}, "
                f"EV15/25={row['expectancy_points_tp15_sl25']:.3f}, "
                f"EV15/30={row['expectancy_points_tp15_sl30']:.3f}"
            )

    if "optimistic" in mode_rows and "pessimistic" in mode_rows:
        _, opt_metrics, _ = build_confusion_and_metrics(mode_rows["optimistic"])
        _, pes_metrics, _ = build_confusion_and_metrics(mode_rows["pessimistic"])
        print("\nOptimistic vs pessimistic delta summary:")
        print(
            "  accuracy_like delta (pess - opt) = "
            f"{(pes_metrics['__overall__']['accuracy_like'] - opt_metrics['__overall__']['accuracy_like']):.6f}"
        )

    print("\nObservation censoring summary:")
    print(
        f"  accepted_touches={obs_stats['summary']['accepted_touches']}, "
        f"rejected_touches={obs_stats['summary']['rejected_touches']}, "
        f"rejection_rate={obs_stats['summary']['rejection_rate']:.6f}"
    )
    for detail in obs_stats["by_group"]:
        print(
            "  "
            f"reason={detail['reason']} session={detail['session']} "
            f"level_type={detail['level_type']} direction={detail['direction']} count={detail['count']}"
        )

    # top observations for TP/SL comparison
    primary_rows = mode_rows[primary_mode]
    ev_1515 = sum(r.get("tp15_sl15_pnl_points", 0.0) for r in primary_rows) / max(1, len(primary_rows))
    ev_1525 = sum(r.get("tp15_sl25_pnl_points", 0.0) for r in primary_rows) / max(1, len(primary_rows))
    ev_1530 = sum(r.get("tp15_sl30_pnl_points", 0.0) for r in primary_rows) / max(1, len(primary_rows))
    print("\nTP/SL top-line expectancy points (per prediction):")
    print(f"  15/15={ev_1515:.4f}, 15/25={ev_1525:.4f}, 15/30={ev_1530:.4f}")
    print("=" * 88 + "\n")

    return detailed_csv, summary_csv


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run prediction analytics on replay data")
    parser.add_argument("--start", default="2025-06-08", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2025-11-11", help="End date YYYY-MM-DD")
    parser.add_argument("--data-dir", default=str(_BACKEND_DIR.parent / "data" / "databento" / "NQ"), help="Replay parquet root")
    parser.add_argument("--model-dir", default=str(_BACKEND_DIR.parent / "data" / "models"), help="Model directory with .cbm")
    parser.add_argument("--output-dir", default=str(_BACKEND_DIR / "analytics_results"), help="Output directory for CSVs")
    parser.add_argument("--resolution-mode", choices=["optimistic", "pessimistic", "both"], default="both")
    parser.add_argument("--min-breakdown-samples", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    run_prediction_analytics(
        data_dir=Path(args.data_dir),
        model_dir=Path(args.model_dir),
        output_dir=Path(args.output_dir),
        start_date=args.start,
        end_date=args.end,
        resolution_mode=args.resolution_mode,
        min_breakdown_samples=args.min_breakdown_samples,
    )


if __name__ == "__main__":
    main()
