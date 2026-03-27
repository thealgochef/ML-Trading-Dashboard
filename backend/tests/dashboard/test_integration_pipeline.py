"""
Integration test — full signal-to-trade pipeline with mock ticks.

Tests the complete wiring from raw ticks through touch detection,
observation windows, CatBoost prediction, trade execution, position
monitoring (TP/SL/DLL), and hard flatten — all without a live data feed.

Each test creates all Phase 1-4 components and wires them with the
same callback pattern used by _create_live_state() in server.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from alpha_lab.dashboard.api.server import DashboardState
from alpha_lab.dashboard.engine.feature_computer import FeatureComputer
from alpha_lab.dashboard.engine.level_engine import LevelEngine
from alpha_lab.dashboard.engine.models import ObservationStatus, TradeDirection
from alpha_lab.dashboard.engine.observation_manager import ObservationManager
from alpha_lab.dashboard.engine.touch_detector import TouchDetector
from alpha_lab.dashboard.model.model_manager import ModelManager
from alpha_lab.dashboard.model.outcome_tracker import OutcomeTracker
from alpha_lab.dashboard.model.prediction_engine import PredictionEngine
from alpha_lab.dashboard.pipeline.price_buffer import PriceBuffer
from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate
from alpha_lab.dashboard.trading import AccountStatus

# All ticks use NY RTH timestamp (14:30 UTC = 09:30 ET during EST)
BASE_TS = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)


# ── Helpers ─────────────────────────────────────────────────────────


def _make_trade(ts_offset_s: float, price: float, size: int = 5) -> TradeUpdate:
    return TradeUpdate(
        timestamp=BASE_TS + timedelta(seconds=ts_offset_s),
        price=Decimal(str(price)),
        size=size,
        aggressor_side="BUY",
        symbol="NQH6",
    )


def _make_bbo(ts_offset_s: float, bid: float, ask: float) -> BBOUpdate:
    return BBOUpdate(
        timestamp=BASE_TS + timedelta(seconds=ts_offset_s),
        bid_price=Decimal(str(bid)),
        bid_size=10,
        ask_price=Decimal(str(ask)),
        ask_size=10,
        symbol="NQH6",
    )


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def catboost_model_path(tmp_path: Path) -> Path:
    """Train and save a synthetic 3-class CatBoost model.

    Class 0 (tradeable_reversal): low time_beyond, high time_within, high absorption
    Class 1 (trap_reversal): medium values
    Class 2 (aggressive_blowthrough): high time_beyond, low time_within, low absorption
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
        iterations=50, depth=3, learning_rate=0.1,
        loss_function="MultiClass", verbose=0, random_seed=42,
        allow_writing_files=False,
    )
    model.fit(x, y)

    path = tmp_path / "test_model.cbm"
    model.save_model(str(path))
    return path


# ── Wiring helper ───────────────────────────────────────────────────


def _create_wired_state(model_path: Path | None, tmp_path: Path):
    """Create a fully wired DashboardState without live data.

    Replicates the same callback chain as _create_live_state() in
    server.py, but without PipelineService or Databento. Returns
    (state, on_trade, on_bbo) — the handler functions that the test
    calls directly with synthetic ticks.
    """
    # PriceBuffer with a seed trade so latest_price is set
    buffer = PriceBuffer(max_duration=timedelta(hours=48))
    seed = TradeUpdate(
        timestamp=BASE_TS - timedelta(seconds=10),
        price=Decimal("20100"),
        size=1,
        aggressor_side="BUY",
        symbol="NQH6",
    )
    buffer.add_trade(seed)

    # Phase 1: Levels
    level_engine = LevelEngine(buffer)

    # Phase 2: Touch + Observation
    feature_computer = FeatureComputer()
    touch_detector = TouchDetector(level_engine)
    observation_manager = ObservationManager(feature_computer)

    # Phase 3: Model + Prediction + Outcome
    model_dir = tmp_path / "models"
    model_dir.mkdir(exist_ok=True)
    model_manager = ModelManager(model_dir)
    if model_path is not None:
        version = model_manager.upload_model(model_path)
        model_manager.activate_model(version["id"])

    prediction_engine = PredictionEngine(model_manager)
    outcome_tracker = OutcomeTracker()

    # Create state with all components
    state = DashboardState(
        level_engine=level_engine,
        touch_detector=touch_detector,
        observation_manager=observation_manager,
        model_manager=model_manager,
        prediction_engine=prediction_engine,
        outcome_tracker=outcome_tracker,
    )

    # ── Wire callbacks (same chain as _create_live_state) ───────

    # 1. Touch → Observation start
    def _on_touch(event):
        observation_manager.start_observation(event)

    touch_detector.on_touch(_on_touch)

    # 2. Observation complete → Prediction
    def _on_observation_complete(window):
        if window.status != ObservationStatus.COMPLETED:
            return
        prediction_engine.predict(window)

    observation_manager.on_observation_complete(_on_observation_complete)

    # 3. Prediction → TradeExecutor + OutcomeTracker + state
    def _on_prediction(prediction):
        pred_data = {
            "event_id": prediction.event_id,
            "predicted_class": prediction.predicted_class,
            "is_executable": prediction.is_executable,
            "probabilities": prediction.probabilities,
            "features": prediction.features,
            "trade_direction": prediction.trade_direction.value,
            "level_price": float(prediction.level_price),
            "model_version": prediction.model_version,
            "timestamp": prediction.timestamp.isoformat(),
        }
        state.last_prediction = pred_data
        state.todays_predictions.append(pred_data)

        outcome_tracker.start_tracking(prediction)

        if prediction.is_executable:
            executor_dict = {
                "is_executable": True,
                "trade_direction": prediction.trade_direction,
                "level_price": prediction.level_price,
            }
            # Use tick timestamp (not wall-clock) to avoid time-of-day test flakiness
            entry_ts = prediction.observation.event.timestamp
            state.trade_executor.on_prediction(
                prediction=executor_dict,
                timestamp=entry_ts,
                current_price=prediction.level_price,
            )

    prediction_engine.on_prediction(_on_prediction)

    # 4. Trade closed → state
    def _on_trade_closed(trade):
        trade_data = {
            "account_id": trade.account_id,
            "direction": trade.direction.value,
            "entry_price": float(trade.entry_price),
            "exit_price": float(trade.exit_price),
            "contracts": trade.contracts,
            "entry_time": trade.entry_time.isoformat(),
            "exit_time": trade.exit_time.isoformat(),
            "pnl": float(trade.pnl),
            "pnl_points": float(trade.pnl_points),
            "exit_reason": trade.exit_reason,
            "group": trade.group,
        }
        state.todays_trades.append(trade_data)

    state.trade_executor.on_trade_closed(_on_trade_closed)

    # 5. Outcome resolved → state
    def _on_outcome_resolved(outcome):
        for pred in state.todays_predictions:
            if pred.get("event_id") == outcome.event_id:
                pred["prediction_correct"] = outcome.prediction_correct
                pred["actual_class"] = outcome.actual_class
                break

    outcome_tracker.on_outcome_resolved(_on_outcome_resolved)

    # ── Bridge handlers (same as _on_trade / _on_bbo in server.py) ──

    def on_trade(trade: TradeUpdate) -> None:
        state.latest_price = float(trade.price)

        # 1. Touch detection
        touch_detector.on_trade(trade)
        # 2. Observation accumulation
        observation_manager.on_trade(trade)
        # 3. Position monitoring (TP/SL/DLL)
        state.position_monitor.on_trade(trade)
        # 4. Outcome tracking (MFE/MAE)
        outcome_tracker.on_trade(trade)
        # 5. Hard flatten check
        state.position_monitor.check_flatten_time(
            trade.timestamp, trade.price,
        )

    def on_bbo(bbo: BBOUpdate) -> None:
        state.latest_bid = float(bbo.bid_price)
        state.latest_ask = float(bbo.ask_price)
        observation_manager.on_bbo(bbo)

    return state, on_trade, on_bbo


# ── Test 1: Full signal pipeline ────────────────────────────────────


def test_full_signal_pipeline_mock_ticks(catboost_model_path, tmp_path):
    """End-to-end: tick → touch → observation → prediction → trade → TP."""
    state, on_trade, on_bbo = _create_wired_state(catboost_model_path, tmp_path)

    # Add 5 accounts: 3 Group A (TP=15 pts), 2 Group B (TP=30 pts)
    for i in range(3):
        state.account_manager.add_account(
            f"A{i + 1}", Decimal("147"), Decimal("85"), "A",
        )
    for i in range(2):
        state.account_manager.add_account(
            f"B{i + 1}", Decimal("147"), Decimal("85"), "B",
        )
    assert len(state.account_manager.get_all_accounts()) == 5

    # Add manual level at 20000.00 (side=LOW since 20000 < latest_price 20100)
    today = BASE_TS.date()
    level = state.level_engine.add_manual_level(Decimal("20000"), today)
    assert level.side.value == "low"
    assert state.touch_detector.active_zone_count == 1

    # ── Approach ticks: no touch yet ────────────────────────────
    for price in [20001, 20000.50, 20000.25]:
        on_trade(_make_trade(ts_offset_s=0, price=price))

    assert state.observation_manager.active_observation is None
    assert state.touch_detector.active_zone_count == 1
    assert len(state.todays_predictions) == 0

    # ── Touch tick: price hits level → observation starts ───────
    on_trade(_make_trade(ts_offset_s=5, price=20000.00, size=10))

    obs = state.observation_manager.active_observation
    assert obs is not None, "Touch should have started an observation"
    assert obs.event.trade_direction == TradeDirection.LONG
    assert obs.status == ObservationStatus.ACTIVE
    assert state.touch_detector.active_zone_count == 0  # zone now spent

    # ── 5-minute observation window: accumulate ticks ───────────
    window_ticks = [
        ("bbo", 30, 19999.75, 20000.25),    # mid = 20000.00
        ("trade", 35, 20000.00, None),
        ("bbo", 60, 20000.25, 20000.75),    # mid = 20000.50
        ("trade", 65, 20000.50, None),
        ("bbo", 120, 20000.75, 20001.25),   # mid = 20001.00
        ("trade", 125, 20001.00, None),
        ("bbo", 180, 20000.00, 20000.50),   # mid = 20000.25
        ("trade", 185, 20000.25, None),
        ("bbo", 240, 20000.50, 20001.00),   # mid = 20000.75
        ("trade", 245, 20000.50, None),
    ]

    for tick in window_ticks:
        if tick[0] == "bbo":
            on_bbo(_make_bbo(ts_offset_s=tick[1], bid=tick[2], ask=tick[3]))
        else:
            on_trade(_make_trade(ts_offset_s=tick[1], price=tick[2]))

    # Still inside the window (touch at t=5, end at t=305)
    assert state.observation_manager.active_observation is not None
    assert len(state.todays_predictions) == 0

    # ── Window completion: first trade past t=305 ───────────────
    on_trade(_make_trade(ts_offset_s=306, price=20001.00))

    # Observation completed and cleared
    assert state.observation_manager.active_observation is None

    # Prediction should have fired
    assert len(state.todays_predictions) == 1
    pred = state.todays_predictions[0]
    assert pred["predicted_class"] == "tradeable_reversal"
    assert pred["is_executable"] is True

    # Features should be non-zero
    features = pred["features"]
    assert features["int_time_within_2pts"] > 0
    assert features["int_absorption_ratio"] > 0

    # All 5 accounts should have LONG positions
    accounts = state.account_manager.get_all_accounts()
    for acct in accounts:
        assert acct.has_position, f"{acct.account_id} should have a position"
        assert acct.current_position.direction == TradeDirection.LONG

    # ── Group A TP hit: +15 points ──────────────────────────────
    on_trade(_make_trade(ts_offset_s=310, price=20015.00))

    group_a = [a for a in accounts if a.group == "A"]
    group_b = [a for a in accounts if a.group == "B"]

    for acct in group_a:
        assert not acct.has_position, f"Group A {acct.account_id} should be closed"

    for acct in group_b:
        assert acct.has_position, f"Group B {acct.account_id} should still be open"

    # 3 Group A trades closed
    assert len(state.todays_trades) == 3
    assert all(t["exit_reason"] == "tp" for t in state.todays_trades)
    assert all(t["group"] == "A" for t in state.todays_trades)

    # ── Group B TP hit: +30 points ──────────────────────────────
    on_trade(_make_trade(ts_offset_s=320, price=20030.00))

    for acct in group_b:
        assert not acct.has_position, f"Group B {acct.account_id} should be closed"

    # 5 total trades
    assert len(state.todays_trades) == 5

    # Balance verification: Group A +$300 each, Group B +$600 each
    for acct in group_a:
        assert acct.balance == Decimal("50300"), (
            f"Group A {acct.account_id} balance: {acct.balance}"
        )
    for acct in group_b:
        assert acct.balance == Decimal("50600"), (
            f"Group B {acct.account_id} balance: {acct.balance}"
        )

    # Outcome tracker should have resolved (MFE hit 30 pts ≥ 25)
    resolved = state.todays_predictions[0]
    assert resolved.get("prediction_correct") is not None, (
        "Outcome should have resolved after price moved 30 pts"
    )
    assert resolved["actual_class"] == "tradeable_reversal"
    assert resolved["prediction_correct"] is True


# ── Test 2: Hard flatten ────────────────────────────────────────────


def test_hard_flatten_fires(tmp_path):
    """All positions close at 15:55 ET (20:55 UTC during EST).

    March 2, 2026 is EST (UTC-5). DST starts March 8.
    """
    buffer = PriceBuffer(max_duration=timedelta(hours=48))
    state = DashboardState(level_engine=LevelEngine(buffer))

    # Add 2 accounts and open positions
    state.account_manager.add_account("A1", Decimal("147"), Decimal("85"), "A")
    state.account_manager.add_account("B1", Decimal("147"), Decimal("85"), "B")

    pred = {
        "is_executable": True,
        "trade_direction": TradeDirection.LONG,
        "level_price": Decimal("20000"),
    }
    state.trade_executor.on_prediction(
        pred, datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )

    accts = state.account_manager.get_all_accounts()
    assert all(a.has_position for a in accts)

    # Track closed trades via executor callback
    closed_trades: list = []
    state.trade_executor.on_trade_closed(lambda t: closed_trades.append(t))

    # Before flatten time — positions stay open (15:54:59 EST = 20:54:59 UTC)
    pre = TradeUpdate(
        timestamp=datetime(2026, 3, 2, 20, 54, 59, tzinfo=UTC),
        price=Decimal("20005"), size=5,
        aggressor_side="BUY", symbol="NQH6",
    )
    state.position_monitor.on_trade(pre)
    state.position_monitor.check_flatten_time(pre.timestamp, pre.price)
    assert all(a.has_position for a in accts)

    # At flatten time — everything closes (15:55:00 EST = 20:55:00 UTC)
    flatten = TradeUpdate(
        timestamp=datetime(2026, 3, 2, 20, 55, 0, tzinfo=UTC),
        price=Decimal("20005"), size=5,
        aggressor_side="BUY", symbol="NQH6",
    )
    state.position_monitor.check_flatten_time(
        flatten.timestamp, flatten.price,
    )

    assert all(not a.has_position for a in accts)
    assert len(closed_trades) == 2
    assert all(t.exit_reason == "flatten" for t in closed_trades)


# ── Test 3: DLL breach ──────────────────────────────────────────────


def test_dll_breach_closes_position(tmp_path):
    """DLL breach closes position and locks account."""
    buffer = PriceBuffer(max_duration=timedelta(hours=48))
    state = DashboardState(level_engine=LevelEngine(buffer))

    # Add one account (Tier 1 DLL = $1,000)
    acct = state.account_manager.add_account(
        "A1", Decimal("147"), Decimal("85"), "A",
    )

    # Open LONG at 20000
    pred = {
        "is_executable": True,
        "trade_direction": TradeDirection.LONG,
        "level_price": Decimal("20000"),
    }
    state.trade_executor.on_prediction(
        pred, datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
    )
    assert acct.has_position
    assert acct.status == AccountStatus.ACTIVE

    # Track closed trades
    closed_trades: list = []
    state.trade_executor.on_trade_closed(lambda t: closed_trades.append(t))

    # Drop 50 points → unrealized = -$1,000 = DLL breach
    trade = TradeUpdate(
        timestamp=datetime(2026, 3, 2, 14, 35, 0, tzinfo=UTC),
        price=Decimal("19950"), size=5,
        aggressor_side="SELL", symbol="NQH6",
    )
    state.position_monitor.on_trade(trade)

    assert not acct.has_position, "Position should be closed on DLL breach"
    assert acct.status == AccountStatus.DLL_LOCKED
    assert len(closed_trades) == 1
    assert closed_trades[0].exit_reason == "dll"
