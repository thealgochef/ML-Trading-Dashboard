"""
TP/SL Sweep — standalone batch runner for backtesting TP/SL configurations.

Runs the full signal-to-trade pipeline (TouchDetector → ObservationManager →
PredictionEngine → TradeExecutor → PositionMonitor) against historical Parquet
data, WITHOUT the FastAPI server or WebSocket layer.

Usage:
    python run_tp_sl_sweep.py                        # All configs, full date range
    python run_tp_sl_sweep.py --start 2025-06-08 --end 2025-06-12  # Date range
    python run_tp_sl_sweep.py --configs "15,15|20,15" # Specific configs only
    python run_tp_sl_sweep.py --compare-dd            # Intraday vs EOD trailing DD

Output: CSV file in backend/sweep_results/<timestamp>_tp_sl_sweep.csv
        With --compare-dd: separate _intraday.csv and _eod.csv files
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
import time as time_mod
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

# Ensure backend/src is on sys.path
_BACKEND_DIR = Path(__file__).resolve().parent
_SRC_DIR = _BACKEND_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from alpha_lab.dashboard.config.settings import DashboardSettings
from alpha_lab.dashboard.engine.feature_computer import FeatureComputer
from alpha_lab.dashboard.engine.level_engine import LevelEngine, _cme_day_start_utc
from alpha_lab.dashboard.engine.models import LevelType, ObservationStatus
from alpha_lab.dashboard.engine.observation_manager import ObservationManager
from alpha_lab.dashboard.engine.touch_detector import TouchDetector, parse_disabled_level_types
from alpha_lab.dashboard.model.model_manager import ModelManager
from alpha_lab.dashboard.model.outcome_tracker import OutcomeTracker
from alpha_lab.dashboard.model.prediction_engine import PredictionEngine
from alpha_lab.dashboard.pipeline.pipeline_service import PipelineService
from alpha_lab.dashboard.pipeline.replay_client import ReplayClient
from alpha_lab.dashboard.pipeline.tick_bar_builder import TickBarBuilder
from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    TradeUpdate,
)
from alpha_lab.dashboard.trading import AccountStatus, TRAILING_DD
from alpha_lab.dashboard.trading.account_manager import AccountManager
from alpha_lab.dashboard.trading.economic_config import EconomicConfig
from alpha_lab.dashboard.trading.economic_tracker import EconomicTracker
from alpha_lab.dashboard.trading.position_monitor import PositionMonitor
from alpha_lab.dashboard.trading.trade_executor import TradeExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(name)s - %(message)s",
)
logger = logging.getLogger("sweep")

# ── Default configs (TP, SL) in NQ points ─────────────────────
DEFAULT_CONFIGS: list[tuple[int, int]] = [
    # Symmetric
    (25, 40), (15, 15), (15,30)
]

_TRAILING_DD_LIMIT = float(TRAILING_DD)


def _auto_load_model(model_manager: ModelManager, model_dir: Path) -> None:
    """Load the CatBoost model (same logic as server.py)."""
    cbm_files = sorted(model_dir.glob("*.cbm"))
    if not cbm_files:
        raise FileNotFoundError(f"No .cbm model in {model_dir}")

    preferred = model_dir / "dashboard_3feature_v1.cbm"
    chosen = preferred if preferred in cbm_files else cbm_files[0]

    version = model_manager.upload_model(chosen)
    model_manager.activate_model(version["id"])
    logger.info("Loaded model: %s", chosen.name)


def _collect_group_results(
    group: str,
    accounts: list,
    trades: list[dict],
    balance_tracking: dict,
    num_accounts: int,
) -> dict:
    """Compute summary metrics for a single group's accounts and trades.

    Returns a dict suitable for one CSV row (without tp/sl/dates/elapsed/ticks).
    """
    group_trades = [t for t in trades if t["group"] == group]
    group_accts = [a for a in accounts if a.group == group]
    blown_count = sum(1 for a in group_accts if a.status == AccountStatus.BLOWN)

    # Peak/trough/max_trailing_dd from running balance tracking
    bt = balance_tracking
    all_peaks = [bt[a.account_id]["peak"] for a in group_accts if a.account_id in bt]
    all_troughs = [bt[a.account_id]["trough"] for a in group_accts if a.account_id in bt]
    peak_balance = max(all_peaks) if all_peaks else 50_000.0
    trough_balance = min(all_troughs) if all_troughs else 50_000.0
    max_trailing_dd = max(
        (bt[a.account_id]["max_dd"] for a in group_accts if a.account_id in bt),
        default=0.0,
    )

    # Sanity check
    if max_trailing_dd > _TRAILING_DD_LIMIT and blown_count == 0:
        logger.warning(
            "SANITY CHECK FAILED [%s]: max_trailing_dd=$%.0f > $%.0f but no accounts blown",
            group, max_trailing_dd, _TRAILING_DD_LIMIT,
        )

    # Trade summary
    total_trades = len(group_trades)
    tp_exits = sum(1 for t in group_trades if t["exit_reason"] == "tp")
    sl_exits = sum(1 for t in group_trades if t["exit_reason"] == "sl")
    blown_exits = sum(1 for t in group_trades if t["exit_reason"] == "blown")
    dll_exits = sum(1 for t in group_trades if t["exit_reason"] == "dll")
    flatten_exits = sum(1 for t in group_trades if t["exit_reason"] == "flatten")
    total_pnl = sum(t["pnl"] for t in group_trades)

    # Signal-level stats (dedup by entry_time within this group)
    signal_results: dict[str, str] = {}
    for t in group_trades:
        key = t.get("entry_time", "")
        if key not in signal_results:
            signal_results[key] = t["exit_reason"]
    signals = len(signal_results)
    signal_wins = sum(1 for r in signal_results.values() if r == "tp")
    signal_losses = signals - signal_wins

    # Commissions (simple: total_trades * commission_per_rt)
    commission_per_rt = EconomicConfig().commission_per_rt
    total_commissions = total_trades * commission_per_rt
    gross_pnl = total_pnl
    trading_friction = total_commissions + blown_count * EconomicConfig().reset_cost
    net_pnl = gross_pnl - trading_friction
    total_account_costs = EconomicConfig().total_account_cost * num_accounts
    ev_economic = 0.0 - total_account_costs - trading_friction + gross_pnl

    return {
        "signals": signals,
        "signal_wins": signal_wins,
        "signal_losses": signal_losses,
        "signal_winrate": round(signal_wins / max(1, signals), 4),
        "total_trades": total_trades,
        "tp_exits": tp_exits,
        "sl_exits": sl_exits,
        "blown_exits": blown_exits,
        "dll_exits": dll_exits,
        "flatten_exits": flatten_exits,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / max(1, total_trades), 2),
        "accounts_blown": blown_count,
        "accounts_surviving": num_accounts - blown_count,
        "peak_balance": round(peak_balance, 2),
        "trough_balance": round(trough_balance, 2),
        "max_trailing_dd": round(max_trailing_dd, 2),
        "gross_pnl": round(gross_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "total_commissions": round(total_commissions, 2),
        "ev_economic": round(ev_economic, 2),
    }


def run_one_config(
    tp: int,
    sl: int,
    model_manager: ModelManager,
    data_dir: Path,
    start_date: str,
    end_date: str,
    compare_dd: bool = False,
    disabled_level_types: set[LevelType] | None = None,
) -> dict | tuple[dict, dict]:
    """Run a single TP/SL configuration through the full replay pipeline.

    When compare_dd=False: returns one dict (group A, intraday DD).
    When compare_dd=True: returns (intraday_dict, eod_dict) for groups A and B.
    """
    # Suppress verbose pipeline logging during batch replay
    logging.getLogger("alpha_lab").setLevel(logging.WARNING)

    mode_str = "INTRADAY vs EOD" if compare_dd else "INTRADAY"
    logger.info("=" * 60)
    logger.info("CONFIG: TP=%d SL=%d | %s | %s to %s", tp, sl, mode_str, start_date, end_date)
    logger.info("=" * 60)

    t0 = time_mod.time()

    # ── Create components ────────────────────────────────────────
    settings = DashboardSettings()
    account_manager = AccountManager()
    trade_executor = TradeExecutor(account_manager)
    position_monitor = PositionMonitor(
        account_manager, trade_executor,
        slippage_points=Decimal(str(args.slippage)),
    )

    # Set TP/SL for group A (always)
    position_monitor.set_group_tp("A", Decimal(str(tp)))
    position_monitor.set_group_sl("A", Decimal(str(sl)))

    # Create Group A accounts: intraday trailing DD
    num_per_group = 5
    for i in range(1, num_per_group + 1):
        account_manager.add_account(
            f"A{i}", Decimal("147"), Decimal("85"), "A", dd_type="intraday",
        )

    if compare_dd:
        # Set TP/SL for group B (same values — only DD mode differs)
        position_monitor.set_group_tp("B", Decimal(str(tp)))
        position_monitor.set_group_sl("B", Decimal(str(sl)))

        # Create Group B accounts: EOD trailing DD
        for i in range(1, num_per_group + 1):
            account_manager.add_account(
                f"B{i}", Decimal("147"), Decimal("85"), "B", dd_type="eod",
            )

    # Create pipeline with ReplayClient
    client = ReplayClient(
        data_dir=data_dir,
        start_date=start_date,
        end_date=end_date,
        speed=9999.0,  # Max speed
    )
    pipeline = PipelineService(settings, client=client)

    # Engine components
    level_engine = LevelEngine(pipeline._buffer)
    touch_detector = TouchDetector(
        level_engine,
        disabled_level_types=disabled_level_types,
    )
    observation_manager = ObservationManager(FeatureComputer())
    prediction_engine = PredictionEngine(model_manager)
    outcome_tracker = OutcomeTracker()
    tick_bar_builder = TickBarBuilder()

    # Economic tracker
    economic_tracker = EconomicTracker(EconomicConfig())

    # ── State tracking (replaces DashboardState) ─────────────────
    state = {
        "tick_count": 0,
        "touch_count": 0,
        "observation_count": 0,
        "prediction_count": 0,
        "trade_exec_count": 0,
        "session_ended": False,
        "latest_price": None,
        "latest_bid": None,
        "latest_ask": None,
        "todays_trades": [],
        "all_blown": False,
        # Per-account running balance tracking: {acct_id: {peak, trough, hwm, max_dd}}
        "balance_tracking": {},
    }

    # ── Wire callbacks (minimal — no WS broadcast) ──────────────

    # Tick bar builder: register on pipeline trades
    pipeline.register_trade_handler(tick_bar_builder.on_trade)

    # Step mode bar-complete (needed for ReplayClient's step mechanism,
    # but we never actually pause — just set the event so it doesn't block)
    def _on_bar_for_step(timeframe, bar):
        client._bar_complete_event.set()
    tick_bar_builder.on_bar_complete(_on_bar_for_step)

    # Touch → Observation
    def _on_touch(event):
        state["touch_count"] += 1
        observation_manager.start_observation(event)

    touch_detector.on_touch(_on_touch)

    # Observation complete → Prediction
    def _on_observation_complete(window):
        state["observation_count"] += 1
        if window.status != ObservationStatus.COMPLETED:
            return
        prediction_engine.predict(window)

    observation_manager.on_observation_complete(_on_observation_complete)

    # Prediction → Trade execution
    def _on_prediction(prediction):
        state["prediction_count"] += 1
        outcome_tracker.start_tracking(prediction)

        if prediction.is_executable:
            state["trade_exec_count"] += 1
            executor_dict = {
                "is_executable": True,
                "trade_direction": prediction.trade_direction,
                "level_price": prediction.level_price,
            }
            market_price = (
                Decimal(str(state["latest_price"]))
                if state["latest_price"]
                else prediction.level_price
            )
            trade_executor.on_prediction(
                prediction=executor_dict,
                timestamp=prediction.timestamp,
                current_price=market_price,
            )

    prediction_engine.on_prediction(_on_prediction)

    # Trade closed → record + economic tracker
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
        state["todays_trades"].append(trade_data)

        acct = account_manager.get_account(trade.account_id)
        economic_tracker.on_trade_closed(trade_data)
        if acct is not None:
            economic_tracker.on_account_update({
                "account_id": acct.account_id,
                "balance": float(acct.balance),
                "status": acct.status.value,
                "has_position": acct.has_position,
                "timestamp": trade_data["exit_time"],
            })

            # Update running balance tracking for this account
            bal = float(acct.balance)
            aid = acct.account_id
            bt = state["balance_tracking"]
            if aid not in bt:
                bt[aid] = {"peak": 50_000.0, "trough": 50_000.0, "hwm": 50_000.0, "max_dd": 0.0}
            if bal > bt[aid]["peak"]:
                bt[aid]["peak"] = bal
            if bal < bt[aid]["trough"]:
                bt[aid]["trough"] = bal
            # Trailing drawdown: track high water mark and largest drop from it
            if bal > bt[aid]["hwm"]:
                bt[aid]["hwm"] = bal
            current_dd = bt[aid]["hwm"] - bal
            if current_dd > bt[aid]["max_dd"]:
                bt[aid]["max_dd"] = current_dd
            # When an account blows from trailing DD, the DD at that moment
            # was definitionally >= TRAILING_DD. The post-close balance
            # doesn't capture this because the position is already exited.
            if trade.exit_reason == "blown":
                blown_dd = _TRAILING_DD_LIMIT
                if blown_dd > bt[aid]["max_dd"]:
                    bt[aid]["max_dd"] = blown_dd

        # Check early termination: all accounts blown
        all_blown = all(
            a.status == AccountStatus.BLOWN
            for a in account_manager.get_all_accounts()
        )
        if all_blown:
            state["all_blown"] = True
            client._stop_flag = True
            logger.info("ALL ACCOUNTS BLOWN — stopping replay early")

    trade_executor.on_trade_closed(_on_trade_closed)

    # Session change → recompute levels (critical for generating levels
    # from completed sessions like Asia, London)
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    def _cme_trading_date(ts_utc):
        """Return CME trading date for a UTC timestamp."""
        ts_et = ts_utc.astimezone(_ET)
        if ts_et.time() >= time(18, 0):
            return (ts_et + timedelta(days=1)).date()
        return ts_et.date()

    _last_reset_trading_date: date | None = None

    def _on_session_change(old_session, new_session, timestamp):
        nonlocal _last_reset_trading_date
        trading_date = _cme_trading_date(timestamp)

        is_cme_day_boundary = (old_session == "post_market" and new_session == "asia")
        is_bootstrap = (_last_reset_trading_date is None)

        # Bootstrap: first visible session callback — init levels only
        if is_bootstrap:
            _last_reset_trading_date = trading_date
            level_engine.reset_daily()
            level_engine.compute_levels(trading_date, current_time=timestamp)
            return

        # True CME day boundary: post_market → asia
        if is_cme_day_boundary and trading_date != _last_reset_trading_date:
            _last_reset_trading_date = trading_date
            logger.info("CME day boundary: %s -> %s (trading_date=%s)",
                        old_session, new_session, trading_date)

            # STEP 0: Force-resolve any remaining open predictions
            if not state["session_ended"]:
                outcome_tracker.on_session_end(timestamp)

            # STEP 1: End-of-day accounting (reads CLOSING day's state)
            acct_snapshots = []
            for acct in account_manager.get_all_accounts():
                if acct._dd_type == "eod" and acct.status not in (
                    AccountStatus.BLOWN, AccountStatus.RETIRED,
                ):
                    acct.update_eod_dd()
                acct.end_day()
                acct_snapshots.append({
                    "account_id": acct.account_id,
                    "daily_pnl": float(acct.daily_pnl),
                    "balance": float(acct.balance),
                    "status": acct.status.value,
                })
            economic_tracker.on_day_end(trading_date.isoformat(), acct_snapshots)

            # STEP 2: Reset accounts for new day
            account_manager.start_new_day()

            # STEP 3: Reset levels for new trading day
            level_engine.reset_daily()
            level_engine.compute_levels(trading_date, current_time=timestamp)

            # STEP 4: Clear pipeline state
            pipeline._buffer.evict()
            state["session_ended"] = False
            return

        # Regular session transition — just recompute levels
        level_engine.compute_levels(trading_date, current_time=timestamp)

    touch_detector.on_session_change(_on_session_change)

    # Bridge: trade tick → full pipeline

    def _on_trade(trade: TradeUpdate) -> None:
        state["tick_count"] += 1
        price = float(trade.price)
        state["latest_price"] = price

        # Preload guard
        if getattr(client, '_preloading', False):
            return

        # Flatten check
        try:
            position_monitor.check_flatten_time(trade.timestamp, trade.price)
        except Exception:
            pass

        # Session end
        if not state["session_ended"]:
            ts_et = trade.timestamp.astimezone(_ET)
            past_flatten = (
                ts_et.hour > 15 or (ts_et.hour == 15 and ts_et.minute >= 55)
            )
            if past_flatten:
                state["session_ended"] = True
                outcome_tracker.on_session_end(trade.timestamp)

        try:
            touch_detector.on_trade(trade)
        except Exception:
            logger.exception("touch_detector failed")

        try:
            observation_manager.on_trade(trade)
        except Exception:
            logger.exception("observation_manager failed")

        try:
            position_monitor.on_trade(trade)
        except Exception:
            logger.exception("position_monitor failed")

        # Economic tracker: throttled (every 500 ticks)
        if state["tick_count"] % 500 == 0:
            try:
                acct_snapshots = []
                for acct in account_manager.get_all_accounts():
                    unrealized = (
                        float(acct.current_position.unrealized_pnl)
                        if acct.has_position else 0.0
                    )
                    acct_snapshots.append({
                        "account_id": acct.account_id,
                        "balance": float(acct.balance),
                        "unrealized_pnl": unrealized,
                    })
                economic_tracker.on_price_update(
                    price=price,
                    timestamp=trade.timestamp.isoformat(),
                    accounts=acct_snapshots,
                )
            except Exception:
                pass

        try:
            outcome_tracker.on_trade(trade)
        except Exception:
            pass

    def _on_bbo(bbo: BBOUpdate) -> None:
        state["latest_bid"] = float(bbo.bid_price)
        state["latest_ask"] = float(bbo.ask_price)

        if getattr(client, '_preloading', False):
            return

        try:
            observation_manager.on_bbo(bbo)
        except Exception:
            pass

    def _on_connection_status(status: ConnectionStatus) -> None:
        observation_manager.on_connection_status(status)

    pipeline.register_trade_handler(_on_trade)
    pipeline.register_bbo_handler(_on_bbo)
    pipeline.register_connection_handler(_on_connection_status)

    # ── File-loaded callback (preload-only level init) ──────────
    def _on_file_loaded(date_str: str) -> None:
        if not client._preloading:
            return  # Visible replay: day reset handled by session change
        trading_date = date.fromisoformat(date_str)
        level_engine.reset_daily()
        day_start_utc = _cme_day_start_utc(trading_date)
        level_engine.compute_levels(trading_date, current_time=day_start_utc)

    client.on_file_loaded(_on_file_loaded)

    # ── Run replay ───────────────────────────────────────────────
    async def _run():
        # pipeline.start() wires client→pipeline callbacks, connects,
        # and starts the replay thread (initially paused in step mode)
        await pipeline.start()

        # Immediately play at max speed (no pausing)
        client._step_mode = False
        client._pause_event.set()

        # Wait for replay thread to finish
        while client._thread is not None and client._thread.is_alive():
            await asyncio.sleep(0.1)

    asyncio.run(_run())

    elapsed = time_mod.time() - t0

    # ── Collect results ──────────────────────────────────────────
    all_accounts = account_manager.get_all_accounts()
    all_trades = state["todays_trades"]
    bt = state["balance_tracking"]

    common = {
        "tp": tp,
        "sl": sl,
        "start_date": start_date,
        "end_date": end_date,
        "elapsed_sec": round(elapsed, 1),
        "ticks": state["tick_count"],
        "early_termination": state["all_blown"],
    }

    if compare_dd:
        intraday_metrics = _collect_group_results("A", all_accounts, all_trades, bt, num_per_group)
        eod_metrics = _collect_group_results("B", all_accounts, all_trades, bt, num_per_group)

        intraday_result = {**common, **intraday_metrics}
        eod_result = {**common, **eod_metrics}

        logger.info(
            "RESULT: TP=%d SL=%d", tp, sl,
        )
        logger.info(
            "  Intraday: %d sigs, %.1f%% WR, $%.0f PnL, %d blown, max_dd=$%.0f",
            intraday_metrics["signals"],
            intraday_metrics["signal_winrate"] * 100,
            intraday_metrics["total_pnl"],
            intraday_metrics["accounts_blown"],
            intraday_metrics["max_trailing_dd"],
        )
        logger.info(
            "  EOD:      %d sigs, %.1f%% WR, $%.0f PnL, %d blown, max_dd=$%.0f",
            eod_metrics["signals"],
            eod_metrics["signal_winrate"] * 100,
            eod_metrics["total_pnl"],
            eod_metrics["accounts_blown"],
            eod_metrics["max_trailing_dd"],
        )

        return (intraday_result, eod_result)

    # Single-group mode (original behavior)
    metrics = economic_tracker.compute_tier1_metrics()
    group_metrics = _collect_group_results("A", all_accounts, all_trades, bt, num_per_group)

    result = {
        **common,
        **group_metrics,
        "payout_conversion_rate": metrics.get("payout_conversion", {}).get("payout_conversion_rate", 0),
        "expected_net_per_cycle": metrics.get("payout_conversion", {}).get("expected_net_per_cycle", 0),
        "ev_economic": metrics.get("friction", {}).get("ev_economic", 0),
    }

    logger.info(
        "RESULT: TP=%d SL=%d | signals=%d win_rate=%.1f%% | PnL=$%.0f | blown=%d | %.1fs",
        tp, sl, group_metrics["signals"], group_metrics["signal_winrate"] * 100,
        group_metrics["total_pnl"], group_metrics["accounts_blown"], elapsed,
    )

    return result


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write a list of dicts to CSV."""
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="TP/SL sweep backtest runner")
    parser.add_argument(
        "--configs",
        type=str,
        default=None,
        help='Pipe-separated TP,SL pairs (e.g. "15,15|20,15")',
    )
    parser.add_argument("--start", type=str, default="2025-06-02")
    parser.add_argument("--end", type=str, default="2025-06-24")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override replay data directory",
    )
    parser.add_argument(
        "--compare-dd",
        action="store_true",
        help="Run intraday vs EOD trailing DD comparison (10 accounts per config)",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.50,
        help="Slippage in NQ points per side for TP/SL exits (default: 0.50 = 2 ticks)",
    )
    parser.add_argument(
        "--disable-levels",
        type=str,
        default="",
        help="Comma-separated level types to disable at touch layer (e.g. pdh,pdl)",
    )
    args = parser.parse_args()
    disabled_level_types = parse_disabled_level_types(args.disable_levels)

    # Parse configs
    if args.configs:
        configs = []
        for pair in args.configs.split("|"):
            tp_str, sl_str = pair.strip().split(",")
            configs.append((int(tp_str.strip()), int(sl_str.strip())))
    else:
        configs = DEFAULT_CONFIGS

    # Data directory
    settings = DashboardSettings()
    data_dir = Path(args.data_dir) if args.data_dir else settings.replay_data_dir
    if not data_dir.is_absolute():
        data_dir = _BACKEND_DIR / data_dir

    mode_str = "compare-dd (intraday vs EOD)" if args.compare_dd else "intraday only"
    logger.info("Sweep: %d configs, dates %s to %s, mode=%s", len(configs), args.start, args.end, mode_str)
    logger.info("Data dir: %s", data_dir)

    # Load model once (reused across all configs)
    model_manager = ModelManager(settings.model_dir)
    _auto_load_model(model_manager, settings.model_dir)

    # Output directory
    out_dir = _BACKEND_DIR / "sweep_results"
    out_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Run all configs
    intraday_results: list[dict] = []
    eod_results: list[dict] = []
    results: list[dict] = []
    total_t0 = time_mod.time()

    for i, (tp, sl) in enumerate(configs, 1):
        logger.info("\n[%d/%d] Starting TP=%d SL=%d", i, len(configs), tp, sl)
        try:
            result = run_one_config(
                tp=tp,
                sl=sl,
                model_manager=model_manager,
                data_dir=data_dir,
                start_date=args.start,
                end_date=args.end,
                compare_dd=args.compare_dd,
                disabled_level_types=disabled_level_types,
            )
            if args.compare_dd:
                intraday_result, eod_result = result
                intraday_results.append(intraday_result)
                eod_results.append(eod_result)
            else:
                results.append(result)
        except Exception:
            logger.exception("Config TP=%d SL=%d FAILED", tp, sl)
            error_row = {"tp": tp, "sl": sl, "error": "FAILED"}
            if args.compare_dd:
                intraday_results.append(error_row)
                eod_results.append(error_row)
            else:
                results.append(error_row)

    total_elapsed = time_mod.time() - total_t0

    # Write CSV(s)
    if args.compare_dd:
        intraday_csv = out_dir / f"{timestamp}_tp_sl_sweep_intraday.csv"
        eod_csv = out_dir / f"{timestamp}_tp_sl_sweep_eod.csv"
        _write_csv(intraday_csv, intraday_results)
        _write_csv(eod_csv, eod_results)
        logger.info("\n" + "=" * 60)
        logger.info("SWEEP COMPLETE: %d configs in %.1fs", len(configs), total_elapsed)
        logger.info("Intraday results: %s", intraday_csv)
        logger.info("EOD results:      %s", eod_csv)
        logger.info("=" * 60)
    else:
        csv_path = out_dir / f"{timestamp}_tp_sl_sweep.csv"
        _write_csv(csv_path, results)
        logger.info("\n" + "=" * 60)
        logger.info("SWEEP COMPLETE: %d configs in %.1fs", len(configs), total_elapsed)
        logger.info("Results: %s", csv_path)
        logger.info("=" * 60)

    # Print summary table
    if args.compare_dd:
        print(f"\n  {'TP':>2} | {'SL':>2} | {'Mode':<9} | {'Sigs':>4} | {'Win%':>5} | {'PnL':>10} | {'Blown':>5} | {'MaxDD':>7}")
        print("  " + "-" * 70)
        for intra, eod in zip(intraday_results, eod_results):
            if "error" in intra:
                print(f"  {intra['tp']:>2} | {intra['sl']:>2} | ERROR")
                continue
            print(
                f"  {intra['tp']:>2} | {intra['sl']:>2} | {'Intraday':<9} | "
                f"{intra['signals']:>4} | {intra['signal_winrate']*100:>4.1f}% | "
                f"${intra['total_pnl']:>8.0f} | {intra['accounts_blown']:>5} | "
                f"${intra['max_trailing_dd']:>6.0f}"
            )
            print(
                f"  {'':>2} | {'':>2} | {'EOD':<9} | "
                f"{eod['signals']:>4} | {eod['signal_winrate']*100:>4.1f}% | "
                f"${eod['total_pnl']:>8.0f} | {eod['accounts_blown']:>5} | "
                f"${eod['max_trailing_dd']:>6.0f}"
            )
    else:
        print("\n  TP | SL | Signals | Win%% | PnL       | Blown | Time")
        print("  " + "-" * 58)
        for r in results:
            if "error" in r:
                print(f"  {r['tp']:>2} | {r['sl']:>2} | ERROR")
                continue
            print(
                f"  {r['tp']:>2} | {r['sl']:>2} | {r['signals']:>7} | "
                f"{r['signal_winrate']*100:>4.1f} | "
                f"${r['total_pnl']:>8.0f} | "
                f"{r['accounts_blown']:>5} | "
                f"{r['elapsed_sec']:>5.1f}s"
            )


if __name__ == "__main__":
    main()
