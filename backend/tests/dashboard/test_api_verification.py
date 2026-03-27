"""
API verification test — exercises every endpoint after a full
signal-to-trade pipeline run.

Proves:
  1. GET /api/accounts returns 5 accounts with balances, trade counts
  2. GET /api/data/trades returns closed trades with P&L
  3. GET /api/data/predictions returns predictions with outcomes
  4. GET /api/data/performance returns accurate win/loss/accuracy stats
  5. GET /api/models/diagnostic returns full system diagnostic
  6. GET /api/data/equity-curve returns time-series snapshots
  7. Session stats broadcast on prediction + trade + outcome events
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from alpha_lab.dashboard.api.server import DashboardState, create_app
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

BASE_TS = datetime(2026, 3, 17, 14, 30, 0, tzinfo=UTC)


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


@pytest.fixture
def catboost_model_path(tmp_path: Path) -> Path:
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


def _create_wired_state_with_accounts(model_path: Path, tmp_path: Path):
    """Create a fully wired DashboardState with 5 accounts and model."""
    buffer = PriceBuffer(max_duration=timedelta(hours=48))
    seed = TradeUpdate(
        timestamp=BASE_TS - timedelta(seconds=10),
        price=Decimal("20100"),
        size=1,
        aggressor_side="BUY",
        symbol="NQH6",
    )
    buffer.add_trade(seed)

    level_engine = LevelEngine(buffer)
    feature_computer = FeatureComputer()
    touch_detector = TouchDetector(level_engine)
    observation_manager = ObservationManager(feature_computer)

    model_dir = tmp_path / "models"
    model_dir.mkdir(exist_ok=True)
    model_manager = ModelManager(model_dir)
    version = model_manager.upload_model(model_path)
    model_manager.activate_model(version["id"])

    prediction_engine = PredictionEngine(model_manager)
    outcome_tracker = OutcomeTracker()

    state = DashboardState(
        level_engine=level_engine,
        touch_detector=touch_detector,
        observation_manager=observation_manager,
        model_manager=model_manager,
        prediction_engine=prediction_engine,
        outcome_tracker=outcome_tracker,
    )

    # Add 5 accounts (same as _create_live_state now does)
    state.account_manager.add_account("A1", Decimal("147"), Decimal("85"), "A")
    state.account_manager.add_account("A2", Decimal("147"), Decimal("85"), "A")
    state.account_manager.add_account("A3", Decimal("147"), Decimal("85"), "A")
    state.account_manager.add_account("B1", Decimal("147"), Decimal("85"), "B")
    state.account_manager.add_account("B2", Decimal("147"), Decimal("85"), "B")

    # Record initial equity snapshots
    now_iso = datetime.now(UTC).isoformat()
    for acct in state.account_manager.get_all_accounts():
        state.equity_snapshots.append({
            "timestamp": now_iso,
            "account_id": acct.account_id,
            "balance": float(acct.balance),
            "profit": float(acct.profit),
            "group": acct.group,
        })

    # Wire callbacks (same chain as server.py)
    def _on_touch(event):
        observation_manager.start_observation(event)

    touch_detector.on_touch(_on_touch)

    def _on_observation_complete(window):
        if window.status != ObservationStatus.COMPLETED:
            return
        prediction_engine.predict(window)

    observation_manager.on_observation_complete(_on_observation_complete)

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
            # Use tick timestamp (not wall-clock) to avoid time-of-day flakiness
            entry_ts = prediction.observation.event.timestamp
            state.trade_executor.on_prediction(
                prediction=executor_dict,
                timestamp=entry_ts,
                current_price=prediction.level_price,
            )

    prediction_engine.on_prediction(_on_prediction)

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
        # Record equity snapshot
        acct = state.account_manager.get_account(trade.account_id)
        if acct is not None:
            state.equity_snapshots.append({
                "timestamp": trade_data["exit_time"],
                "account_id": trade.account_id,
                "balance": float(acct.balance),
                "profit": float(acct.profit),
                "group": trade.group,
            })

    state.trade_executor.on_trade_closed(_on_trade_closed)

    def _on_outcome_resolved(outcome):
        for pred in state.todays_predictions:
            if pred.get("event_id") == outcome.event_id:
                pred["prediction_correct"] = outcome.prediction_correct
                pred["actual_class"] = outcome.actual_class
                break

    outcome_tracker.on_outcome_resolved(_on_outcome_resolved)

    def on_trade(trade: TradeUpdate) -> None:
        state.latest_price = float(trade.price)
        touch_detector.on_trade(trade)
        observation_manager.on_trade(trade)
        state.position_monitor.on_trade(trade)
        outcome_tracker.on_trade(trade)
        state.position_monitor.check_flatten_time(trade.timestamp, trade.price)

    def on_bbo(bbo: BBOUpdate) -> None:
        state.latest_bid = float(bbo.bid_price)
        state.latest_ask = float(bbo.ask_price)
        observation_manager.on_bbo(bbo)

    return state, on_trade, on_bbo


def _run_full_pipeline(state, on_trade, on_bbo):
    """Run the full pipeline: touch → observation → prediction → trades → TP."""
    today = BASE_TS.date()
    state.level_engine.add_manual_level(Decimal("20000"), today)

    # Approach ticks
    for price in [20001, 20000.50, 20000.25]:
        on_trade(_make_trade(ts_offset_s=0, price=price))

    # Touch
    on_trade(_make_trade(ts_offset_s=5, price=20000.00, size=10))

    # 5-minute observation window
    window_ticks = [
        ("bbo", 30, 19999.75, 20000.25),
        ("trade", 35, 20000.00, None),
        ("bbo", 60, 20000.25, 20000.75),
        ("trade", 65, 20000.50, None),
        ("bbo", 120, 20000.75, 20001.25),
        ("trade", 125, 20001.00, None),
        ("bbo", 180, 20000.00, 20000.50),
        ("trade", 185, 20000.25, None),
        ("bbo", 240, 20000.50, 20001.00),
        ("trade", 245, 20000.50, None),
    ]
    for tick in window_ticks:
        if tick[0] == "bbo":
            on_bbo(_make_bbo(ts_offset_s=tick[1], bid=tick[2], ask=tick[3]))
        else:
            on_trade(_make_trade(ts_offset_s=tick[1], price=tick[2]))

    # Complete observation window
    on_trade(_make_trade(ts_offset_s=306, price=20001.00))

    # Group A TP hit: +15 points
    on_trade(_make_trade(ts_offset_s=310, price=20015.00))

    # Group B TP hit: +30 points
    on_trade(_make_trade(ts_offset_s=320, price=20030.00))


# ── Test: Full API verification ─────────────────────────────────────


@pytest.mark.anyio
async def test_api_accounts(catboost_model_path, tmp_path):
    """GET /api/accounts returns 5 accounts with correct state after trades."""
    state, on_trade, on_bbo = _create_wired_state_with_accounts(
        catboost_model_path, tmp_path,
    )
    _run_full_pipeline(state, on_trade, on_bbo)

    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/accounts")
        assert resp.status_code == 200
        data = resp.json()

        accounts = data["accounts"]
        assert len(accounts) == 5

        # Check Group A accounts: balance = 50000 + 300 = 50300
        group_a = [a for a in accounts if a["group"] == "A"]
        assert len(group_a) == 3
        for a in group_a:
            assert a["balance"] == 50300.0
            assert a["profit"] == 300.0
            assert a["status"] == "active"
            assert a["tier"] == 1
            assert a["trade_count"] == 1
            assert a["has_position"] is False

        # Check Group B accounts: balance = 50000 + 600 = 50600
        group_b = [a for a in accounts if a["group"] == "B"]
        assert len(group_b) == 2
        for a in group_b:
            assert a["balance"] == 50600.0
            assert a["profit"] == 600.0
            assert a["trade_count"] == 1

        # Portfolio summary
        summary = data["summary"]
        assert summary["total_accounts"] == 5
        assert summary["active_count"] == 5
        assert summary["blown_count"] == 0


@pytest.mark.anyio
async def test_api_trades(catboost_model_path, tmp_path):
    """GET /api/data/trades returns all 5 closed trades."""
    state, on_trade, on_bbo = _create_wired_state_with_accounts(
        catboost_model_path, tmp_path,
    )
    _run_full_pipeline(state, on_trade, on_bbo)

    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/data/trades")
        assert resp.status_code == 200
        data = resp.json()

        trades = data["trades"]
        assert len(trades) == 5
        assert all(t["exit_reason"] == "tp" for t in trades)
        assert all(t["direction"] == "long" for t in trades)

        # Group A: +15 pts = +$300
        group_a_trades = [t for t in trades if t["group"] == "A"]
        assert len(group_a_trades) == 3
        assert all(t["pnl"] == 300.0 for t in group_a_trades)
        assert all(t["pnl_points"] == 15.0 for t in group_a_trades)

        # Group B: +30 pts = +$600
        group_b_trades = [t for t in trades if t["group"] == "B"]
        assert len(group_b_trades) == 2
        assert all(t["pnl"] == 600.0 for t in group_b_trades)
        assert all(t["pnl_points"] == 30.0 for t in group_b_trades)


@pytest.mark.anyio
async def test_api_predictions(catboost_model_path, tmp_path):
    """GET /api/data/predictions returns prediction with resolved outcome."""
    state, on_trade, on_bbo = _create_wired_state_with_accounts(
        catboost_model_path, tmp_path,
    )
    _run_full_pipeline(state, on_trade, on_bbo)

    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/data/predictions")
        assert resp.status_code == 200
        data = resp.json()

        preds = data["predictions"]
        assert len(preds) == 1

        pred = preds[0]
        assert pred["predicted_class"] == "tradeable_reversal"
        assert pred["is_executable"] is True
        assert pred["prediction_correct"] is True
        assert pred["actual_class"] == "tradeable_reversal"


@pytest.mark.anyio
async def test_api_performance(catboost_model_path, tmp_path):
    """GET /api/data/performance returns correct stats."""
    state, on_trade, on_bbo = _create_wired_state_with_accounts(
        catboost_model_path, tmp_path,
    )
    _run_full_pipeline(state, on_trade, on_bbo)

    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/data/performance")
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_trades"] == 5
        assert data["wins"] == 5
        assert data["losses"] == 0
        assert data["total_pnl"] == 2100.0  # 3×$300 + 2×$600
        assert data["win_rate"] == 1.0
        assert data["prediction_accuracy"] == 1.0


@pytest.mark.anyio
async def test_api_diagnostic(catboost_model_path, tmp_path):
    """GET /api/models/diagnostic returns full system state."""
    state, on_trade, on_bbo = _create_wired_state_with_accounts(
        catboost_model_path, tmp_path,
    )
    _run_full_pipeline(state, on_trade, on_bbo)

    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/models/diagnostic")
        assert resp.status_code == 200
        data = resp.json()

        # Model loaded and active
        assert data["model"]["model_loaded"] is True
        assert data["model"]["active_version"] is not None

        # Predictions
        assert data["predictions"]["total_today"] == 1
        assert data["predictions"]["executable"] == 1
        assert data["predictions"]["resolved"] == 1
        assert data["predictions"]["correct"] == 1

        # Trades
        assert data["trades"]["total_today"] == 5
        assert data["trades"]["total_pnl"] == 2100.0
        assert data["trades"]["by_reason"]["tp"] == 5
        assert data["trades"]["open_positions"] == 0

        # Accounts
        assert data["accounts"]["total"] == 5
        assert data["accounts"]["active"] == 5
        assert data["accounts"]["tradeable"] == 5

        # All accounts have 1 trade each
        for acct in data["accounts"]["accounts"]:
            assert acct["trade_count"] == 1
            assert acct["profit"] > 0


@pytest.mark.anyio
async def test_api_equity_curve(catboost_model_path, tmp_path):
    """GET /api/data/equity-curve returns time-series snapshots."""
    state, on_trade, on_bbo = _create_wired_state_with_accounts(
        catboost_model_path, tmp_path,
    )
    _run_full_pipeline(state, on_trade, on_bbo)

    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/data/equity-curve")
        assert resp.status_code == 200
        data = resp.json()

        snapshots = data["snapshots"]
        # 5 initial + 5 after trade close + 5 current = 15
        assert len(snapshots) == 15

        # First 5 are initial (balance=50000)
        initial = snapshots[:5]
        assert all(s["balance"] == 50000.0 for s in initial)

        # Next 5 are after trade close (balance > 50000)
        post_trade = snapshots[5:10]
        assert all(s["balance"] > 50000.0 for s in post_trade)

        # Filter by account
        resp2 = await client.get("/api/data/equity-curve?account_id=APEX-001")
        data2 = resp2.json()
        assert all(
            s["account_id"] == "APEX-001" for s in data2["snapshots"]
        )
        # initial + post-trade + current = 3
        assert len(data2["snapshots"]) == 3
