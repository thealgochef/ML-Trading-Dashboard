"""
Strategy Comparison — run 4 strategies on the same historical replay data.

Strategy A (Mirror):   5 intraday accounts, 15/15 TP/SL, standard TradeExecutor
Strategy B (Fixed):    5 intraday accounts, 15/30 TP/SL, standard TradeExecutor
Strategy C (RW-Intra): 5 intraday accounts, RegimeWaveExecutor, regime-adaptive TP/SL
Strategy D (RW-EOD):   5 EOD accounts, RegimeWaveExecutor, regime-adaptive TP/SL + compounding

Usage:
    python run_strategy_comparison.py --start 2025-06-08 --end 2025-11-11
    python run_strategy_comparison.py --strategies "A,C"   # subset only
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time as time_mod
from dataclasses import dataclass, field
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
from alpha_lab.dashboard.engine.models import ObservationStatus
from alpha_lab.dashboard.engine.observation_manager import ObservationManager
from alpha_lab.dashboard.engine.touch_detector import TouchDetector
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
from alpha_lab.dashboard.trading.regime_wave_executor import RegimeWaveExecutor
from alpha_lab.dashboard.trading.trade_executor import TradeExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(name)s - %(message)s",
)
logger = logging.getLogger("strategy_comparison")

_TRAILING_DD_LIMIT = float(TRAILING_DD)

from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")


# ── Strategy configuration ────────────────────────────────────────

@dataclass
class StrategyConfig:
    name: str              # "A", "B", "C", "D"
    label: str             # "Mirror", "Fixed", "RW-Intra", "RW-EOD"
    tp: int                # Group-level TP (A/B only; C/D use regime)
    sl: int                # Group-level SL (A/B only; C/D use regime)
    dd_type: str           # "intraday" or "eod"
    use_regime_wave: bool
    enable_compounding: bool
    cost_per_account: float  # $99 intraday, $119 EOD
    wave_assignments: dict[int, str] | None = None


STRATEGIES = [
    StrategyConfig(
        "A", "Mirror", 15, 15, "intraday",
        use_regime_wave=False, enable_compounding=False,
        cost_per_account=99, wave_assignments=None,
    ),
    StrategyConfig(
        "B", "Fixed", 15, 30, "intraday",
        use_regime_wave=False, enable_compounding=False,
        cost_per_account=99, wave_assignments=None,
    ),
    StrategyConfig(
        "C", "RW-Intra", 15, 30, "intraday",
        use_regime_wave=True, enable_compounding=False,
        cost_per_account=99,
        wave_assignments={1: "scout", 2: "scout", 3: "confirmer", 4: "sniper", 5: "sniper"},
    ),
    StrategyConfig(
        "D", "RW-EOD", 15, 30, "eod",
        use_regime_wave=True, enable_compounding=True,
        cost_per_account=119,
        wave_assignments={1: "scout", 2: "scout", 3: "confirmer", 4: "sniper", 5: "sniper"},
    ),
]


def _auto_load_model(model_manager: ModelManager, model_dir: Path) -> None:
    """Load the CatBoost model."""
    cbm_files = sorted(model_dir.glob("*.cbm"))
    if not cbm_files:
        raise FileNotFoundError(f"No .cbm model in {model_dir}")

    preferred = model_dir / "dashboard_3feature_v1.cbm"
    chosen = preferred if preferred in cbm_files else cbm_files[0]

    version = model_manager.upload_model(chosen)
    model_manager.activate_model(version["id"])
    logger.info("Loaded model: %s", chosen.name)


def _cme_trading_date(ts_utc):
    """Return CME trading date for a UTC timestamp."""
    ts_et = ts_utc.astimezone(_ET)
    if ts_et.time() >= time(18, 0):
        return (ts_et + timedelta(days=1)).date()
    return ts_et.date()


# ── Run one strategy ──────────────────────────────────────────────

def run_one_strategy(
    config: StrategyConfig,
    model_manager: ModelManager,
    data_dir: Path,
    start_date: str,
    end_date: str,
) -> dict:
    """Run a single strategy through the full replay pipeline."""
    # Suppress verbose pipeline logging during batch replay
    logging.getLogger("alpha_lab").setLevel(logging.WARNING)

    logger.info("=" * 60)
    logger.info(
        "STRATEGY %s (%s) | TP=%d SL=%d | dd=%s | regime=%s compound=%s",
        config.name, config.label, config.tp, config.sl,
        config.dd_type, config.use_regime_wave, config.enable_compounding,
    )
    logger.info("=" * 60)

    t0 = time_mod.time()
    num_accounts = 5

    # ── Create components ────────────────────────────────────────
    settings = DashboardSettings()
    account_manager = AccountManager()
    trade_executor = TradeExecutor(account_manager)
    position_monitor = PositionMonitor(account_manager, trade_executor)

    # Set group-level TP/SL (used by A/B; C/D override per-account)
    position_monitor.set_group_tp(config.name, Decimal(str(config.tp)))
    position_monitor.set_group_sl(config.name, Decimal(str(config.sl)))

    # Create accounts
    for i in range(1, num_accounts + 1):
        acct = account_manager.add_account(
            f"{config.name}{i}",
            Decimal("20"),
            Decimal("79"),
            config.name,
            dd_type=config.dd_type,
        )
        if config.wave_assignments:
            acct.wave = config.wave_assignments[i]

    # RegimeWaveExecutor (C/D only)
    regime_executor: RegimeWaveExecutor | None = None
    if config.use_regime_wave:
        regime_executor = RegimeWaveExecutor(
            account_manager=account_manager,
            position_monitor=position_monitor,
            enable_eod_compounding=config.enable_compounding,
        )

    # Replay client + pipeline
    client = ReplayClient(
        data_dir=data_dir,
        start_date=start_date,
        end_date=end_date,
        speed=9999.0,
    )
    pipeline = PipelineService(settings, client=client)

    # Engine components
    level_engine = LevelEngine(pipeline._buffer)
    touch_detector = TouchDetector(level_engine)
    observation_manager = ObservationManager(FeatureComputer())
    prediction_engine = PredictionEngine(model_manager)
    outcome_tracker = OutcomeTracker()
    tick_bar_builder = TickBarBuilder()

    # Economic tracker
    economic_tracker = EconomicTracker(EconomicConfig())

    # ── State tracking ────────────────────────────────────────────
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
        "balance_tracking": {},
        "total_payouts": [],
        "trading_days": 0,
    }

    def _reset_dd_tracking_after_payout(account_id: str) -> None:
        """Reset DD tracking baseline after a payout withdrawal.

        Payouts are cash withdrawals, not trading losses. If we keep the old
        HWM baseline across a withdrawal, reported max_dd can be overstated
        relative to Apex trailing-DD risk. After a payout, reset HWM/trough
        to the new balance so subsequent DD reflects trading drawdown only.
        """
        acct = account_manager.get_account(account_id)
        if acct is None:
            return

        bal = float(acct.balance)
        bt = state["balance_tracking"]
        if account_id not in bt:
            bt[account_id] = {
                "peak": bal,
                "trough": bal,
                "hwm": bal,
                "max_dd": 0.0,
            }
            return

        bt[account_id]["hwm"] = bal
        bt[account_id]["trough"] = bal
        if bal > bt[account_id]["peak"]:
            bt[account_id]["peak"] = bal

    # ── Wire callbacks ────────────────────────────────────────────

    pipeline.register_trade_handler(tick_bar_builder.on_trade)

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

    # Prediction → Trade execution (branches on strategy type)
    def _on_prediction(prediction):
        state["prediction_count"] += 1
        outcome_tracker.start_tracking(prediction)

        if not prediction.is_executable:
            return

        state["trade_exec_count"] += 1
        market_price = (
            Decimal(str(state["latest_price"]))
            if state["latest_price"]
            else prediction.level_price
        )

        if regime_executor is not None:
            # Strategies C/D: per-account regime/wave logic
            regime_executor.on_prediction(
                prediction=prediction,
                current_price=market_price,
                timestamp=prediction.timestamp,
            )
        else:
            # Strategies A/B: standard TradeExecutor
            executor_dict = {
                "is_executable": True,
                "trade_direction": prediction.trade_direction,
                "level_price": prediction.level_price,
            }
            trade_executor.on_prediction(
                prediction=executor_dict,
                timestamp=prediction.timestamp,
                current_price=market_price,
            )

    prediction_engine.on_prediction(_on_prediction)

    # Trade closed → record + economic tracker + regime executor
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

            # Running balance tracking
            bal = float(acct.balance)
            aid = acct.account_id
            bt = state["balance_tracking"]
            if aid not in bt:
                bt[aid] = {"peak": 50_000.0, "trough": 50_000.0, "hwm": 50_000.0, "max_dd": 0.0}
            if bal > bt[aid]["peak"]:
                bt[aid]["peak"] = bal
            if bal < bt[aid]["trough"]:
                bt[aid]["trough"] = bal
            if bal > bt[aid]["hwm"]:
                bt[aid]["hwm"] = bal
            current_dd = bt[aid]["hwm"] - bal
            if current_dd > bt[aid]["max_dd"]:
                bt[aid]["max_dd"] = current_dd
            if trade.exit_reason == "blown":
                if _TRAILING_DD_LIMIT > bt[aid]["max_dd"]:
                    bt[aid]["max_dd"] = _TRAILING_DD_LIMIT

        # Forward to RegimeWaveExecutor
        if regime_executor is not None:
            regime_executor.on_trade_closed(trade)

        # Early termination
        all_blown = all(
            a.status == AccountStatus.BLOWN
            for a in account_manager.get_all_accounts()
        )
        if all_blown:
            state["all_blown"] = True
            client._stop_flag = True
            logger.info("[%s] ALL ACCOUNTS BLOWN — stopping early", config.name)

    trade_executor.on_trade_closed(_on_trade_closed)

    # Session change → recompute levels + CME day boundary
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
            logger.info("[%s] CME day boundary: %s -> %s (trading_date=%s)",
                        config.name, old_session, new_session, trading_date)

            # STEP 0: Force-resolve any remaining open predictions
            if not state["session_ended"]:
                outcome_tracker.on_session_end(timestamp)

            # STEP 1: End-of-day accounting (reads CLOSING day's state)
            state["trading_days"] += 1

            for acct in account_manager.get_all_accounts():
                if acct._dd_type == "eod" and acct.status not in (
                    AccountStatus.BLOWN, AccountStatus.RETIRED,
                ):
                    acct.update_eod_dd()

            if regime_executor is not None:
                regime_executor.end_day()
                payouts = regime_executor.check_payouts()
                state["total_payouts"].extend(payouts)
                for payout_acct_id, _ in payouts:
                    _reset_dd_tracking_after_payout(payout_acct_id)
            else:
                for acct in account_manager.get_all_accounts():
                    acct.end_day()
                for acct in account_manager.get_all_accounts():
                    if not acct.payout_eligible:
                        continue
                    available = acct.balance - Decimal("52100")
                    cap = acct.max_payout_amount
                    amount = min(available, cap)
                    if amount < Decimal("500"):
                        continue
                    if acct.request_payout(amount):
                        state["total_payouts"].append((acct.account_id, amount))
                        _reset_dd_tracking_after_payout(acct.account_id)
                        logger.info(
                            "[%s] Payout: account=%s, amount=$%.2f, payout_number=%d",
                            config.name, acct.account_id, float(amount),
                            acct.payout_number,
                        )

            acct_snapshots = []
            for acct in account_manager.get_all_accounts():
                acct_snapshots.append({
                    "account_id": acct.account_id,
                    "daily_pnl": float(acct.daily_pnl),
                    "balance": float(acct.balance),
                    "status": acct.status.value,
                })
            economic_tracker.on_day_end(trading_date.isoformat(), acct_snapshots)

            # STEP 2: Reset accounts for new day
            account_manager.start_new_day()
            if regime_executor is not None:
                regime_executor.start_new_day()

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

    # Trade tick → full pipeline
    def _on_trade(trade: TradeUpdate) -> None:
        state["tick_count"] += 1
        price = float(trade.price)
        state["latest_price"] = price

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
            if ts_et.hour > 15 or (ts_et.hour == 15 and ts_et.minute >= 55):
                state["session_ended"] = True
                outcome_tracker.on_session_end(trade.timestamp)

        try:
            touch_detector.on_trade(trade)
        except Exception:
            logger.exception("[%s] touch_detector failed", config.name)

        try:
            observation_manager.on_trade(trade)
        except Exception:
            logger.exception("[%s] observation_manager failed", config.name)

        try:
            position_monitor.on_trade(trade)
        except Exception:
            logger.exception("[%s] position_monitor failed", config.name)

        # RegimeWaveExecutor: process pending confirmers on each tick
        if regime_executor is not None:
            try:
                regime_executor.on_tick(trade.price, trade.timestamp)
            except Exception:
                logger.exception("[%s] regime_executor.on_tick failed", config.name)

        # Economic tracker: throttled
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

    # ── Run replay ────────────────────────────────────────────────
    async def _run():
        await pipeline.start()
        client._step_mode = False
        client._pause_event.set()
        while client._thread is not None and client._thread.is_alive():
            await asyncio.sleep(0.1)

    asyncio.run(_run())

    elapsed = time_mod.time() - t0

    # ── Collect results ───────────────────────────────────────────
    all_accounts = account_manager.get_all_accounts()
    all_trades = state["todays_trades"]
    bt = state["balance_tracking"]

    blown_count = sum(1 for a in all_accounts if a.status == AccountStatus.BLOWN)

    # Peak/trough/max_trailing_dd from running balance tracking
    all_peaks = [bt[a.account_id]["peak"] for a in all_accounts if a.account_id in bt]
    all_troughs = [bt[a.account_id]["trough"] for a in all_accounts if a.account_id in bt]
    peak_balance = max(all_peaks) if all_peaks else 50_000.0
    trough_balance = min(all_troughs) if all_troughs else 50_000.0
    max_trailing_dd = max(
        (bt[a.account_id]["max_dd"] for a in all_accounts if a.account_id in bt),
        default=0.0,
    )

    # Trade summary
    total_trades = len(all_trades)
    tp_exits = sum(1 for t in all_trades if t["exit_reason"] == "tp")
    sl_exits = sum(1 for t in all_trades if t["exit_reason"] == "sl")
    blown_exits = sum(1 for t in all_trades if t["exit_reason"] == "blown")
    dll_exits = sum(1 for t in all_trades if t["exit_reason"] == "dll")
    flatten_exits = sum(1 for t in all_trades if t["exit_reason"] == "flatten")
    total_pnl = sum(t["pnl"] for t in all_trades)

    # Signal-level stats: use pipeline prediction count (not entry_time dedup,
    # which breaks for C/D where confirmers fill at a different timestamp)
    signals = state["trade_exec_count"]

    # Signal wins: count from scout accounts only. Scouts take every signal
    # and enter at prediction time, so their TP count maps 1:1 to winning
    # predictions. For A/B (no waves), all accounts are equivalent so we
    # pick the first account as the "scout" proxy.
    # Note: compound trades fire on real signals (same entry_time), so
    # the dedup naturally prevents double-counting. Compound stats are
    # tracked separately via regime_executor.stats.
    scout_ids = {a.account_id for a in all_accounts if a.wave == "scout"}
    if not scout_ids:
        # Strategies A/B: use first account as proxy (all identical)
        if all_accounts:
            scout_ids = {all_accounts[0].account_id}
    scout_trades = [t for t in all_trades if t["account_id"] in scout_ids]
    # Dedup by entry_time within scouts (all scouts share the same entry_time)
    scout_signal_results: dict[str, str] = {}
    for t in scout_trades:
        key = t.get("entry_time", "")
        if key not in scout_signal_results:
            scout_signal_results[key] = t["exit_reason"]
    signal_wins = sum(1 for r in scout_signal_results.values() if r == "tp")

    # Costs
    total_cost = config.cost_per_account * num_accounts

    # Payouts
    total_payouts_count = len(state["total_payouts"])
    total_extracted = sum(float(amt) for _, amt in state["total_payouts"])

    # Net ROI
    net_profit = total_extracted - total_cost
    roi = net_profit / total_cost if total_cost > 0 else 0.0

    # Time to first payout
    first_payout_day = None
    if state["total_payouts"]:
        # Payouts happen at day boundaries; approximate by trading days
        first_payout_day = state["trading_days"]  # rough upper bound

    # Per-wave stats (C/D only)
    per_wave_stats = {}
    if config.wave_assignments:
        wave_map = {}
        for acct in all_accounts:
            wave_map[acct.account_id] = acct.wave
        for wave_name in ("scout", "confirmer", "sniper"):
            wave_acct_ids = {a.account_id for a in all_accounts if a.wave == wave_name}
            wave_trades = [t for t in all_trades if t["account_id"] in wave_acct_ids]
            wave_wins = sum(1 for t in wave_trades if t["exit_reason"] == "tp")
            per_wave_stats[wave_name] = {
                "trades": len(wave_trades),
                "wins": wave_wins,
                "winrate": round(wave_wins / max(1, len(wave_trades)), 4),
                "pnl": round(sum(t["pnl"] for t in wave_trades), 2),
            }

    # RegimeWaveExecutor stats
    rw_stats = regime_executor.stats if regime_executor else {}

    # Final account balances
    final_balances = [float(a.balance) for a in all_accounts]
    avg_final_balance = sum(final_balances) / max(1, len(final_balances))

    result = {
        "strategy": config.name,
        "label": config.label,
        "dd_type": config.dd_type,
        "tp": config.tp,
        "sl": config.sl,
        "start_date": start_date,
        "end_date": end_date,
        "elapsed_sec": round(elapsed, 1),
        "ticks": state["tick_count"],
        "trading_days": state["trading_days"],
        "signals": signals,
        "signal_wins": signal_wins,
        "signal_losses": signals - signal_wins,
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
        "avg_final_balance": round(avg_final_balance, 2),
        "cost_total": total_cost,
        "total_payouts_count": total_payouts_count,
        "total_extracted": round(total_extracted, 2),
        "net_profit": round(net_profit, 2),
        "roi": round(roi, 4),
        "early_termination": state["all_blown"],
        # RW-specific stats
        "confirmer_fills": rw_stats.get("confirmer_fills", 0),
        "confirmer_cancels": rw_stats.get("confirmer_cancels", 0),
        "compound_trades": rw_stats.get("compound_trades", 0),
        "compound_wins": rw_stats.get("compound_wins", 0),
    }

    # Add per-wave stats as flat columns
    for wave_name in ("scout", "confirmer", "sniper"):
        ws = per_wave_stats.get(wave_name, {})
        result[f"{wave_name}_trades"] = ws.get("trades", 0)
        result[f"{wave_name}_wins"] = ws.get("wins", 0)
        result[f"{wave_name}_winrate"] = ws.get("winrate", 0.0)
        result[f"{wave_name}_pnl"] = ws.get("pnl", 0.0)

    logger.info(
        "RESULT [%s %s]: %d signals, %.1f%% WR, $%.0f PnL, %d blown, "
        "%d payouts ($%.0f extracted), ROI=%.1f%%",
        config.name, config.label, signals,
        result["signal_winrate"] * 100, total_pnl, blown_count,
        total_payouts_count, total_extracted, roi * 100,
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
    parser = argparse.ArgumentParser(description="Strategy comparison backtest")
    parser.add_argument("--start", type=str, default="2025-06-08")
    parser.add_argument("--end", type=str, default="2025-11-11")
    parser.add_argument(
        "--strategies",
        type=str,
        default=None,
        help='Comma-separated strategy names (e.g. "A,C")',
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=None,
        help="Override replay data directory",
    )
    args = parser.parse_args()

    # Filter strategies
    if args.strategies:
        selected = {s.strip().upper() for s in args.strategies.split(",")}
        strategies = [s for s in STRATEGIES if s.name in selected]
    else:
        strategies = STRATEGIES

    if not strategies:
        logger.error("No strategies selected")
        return

    # Data directory
    settings = DashboardSettings()
    data_dir = Path(args.data_dir) if args.data_dir else settings.replay_data_dir
    if not data_dir.is_absolute():
        data_dir = _BACKEND_DIR / data_dir

    logger.info(
        "Strategy comparison: %d strategies, dates %s to %s",
        len(strategies), args.start, args.end,
    )
    logger.info("Data dir: %s", data_dir)

    # Load model once
    model_manager = ModelManager(settings.model_dir)
    _auto_load_model(model_manager, settings.model_dir)

    # Output directory
    out_dir = _BACKEND_DIR / "sweep_results"
    out_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Run all strategies
    results: list[dict] = []
    total_t0 = time_mod.time()

    for i, config in enumerate(strategies, 1):
        logger.info(
            "\n[%d/%d] Starting Strategy %s (%s)",
            i, len(strategies), config.name, config.label,
        )
        try:
            result = run_one_strategy(
                config=config,
                model_manager=model_manager,
                data_dir=data_dir,
                start_date=args.start,
                end_date=args.end,
            )
            results.append(result)
        except Exception:
            logger.exception("Strategy %s FAILED", config.name)
            results.append({"strategy": config.name, "label": config.label, "error": "FAILED"})

    total_elapsed = time_mod.time() - total_t0

    # Write per-strategy CSVs
    for result in results:
        if "error" not in result:
            csv_path = out_dir / f"{timestamp}_strategy_{result['strategy']}.csv"
            _write_csv(csv_path, [result])

    # Write comparison summary CSV
    comparison_csv = out_dir / f"{timestamp}_strategy_comparison.csv"
    _write_csv(comparison_csv, results)

    logger.info("\n" + "=" * 60)
    logger.info(
        "COMPARISON COMPLETE: %d strategies in %.1fs",
        len(strategies), total_elapsed,
    )
    logger.info("Results: %s", comparison_csv)
    logger.info("=" * 60)

    # Print summary table
    print(
        f"\n  {'Strategy':<10} | {'Cost':>6} | {'PnL':>9} | {'Extracted':>9} | "
        f"{'Net Profit':>10} | {'ROI':>6} | {'Blown':>5} | "
        f"{'Payouts':>7} | {'Signals':>7} | {'Win%':>5}"
    )
    print("  " + "-" * 102)
    for r in results:
        if "error" in r:
            print(f"  {r['strategy']:<2} {r['label']:<7} | ERROR")
            continue
        roi_str = f"{r['roi']*100:.0f}%" if r['roi'] != 0 else "-"
        print(
            f"  {r['strategy']} {r['label']:<7} | "
            f"${r['cost_total']:>5.0f} | "
            f"${r['total_pnl']:>8.0f} | "
            f"${r['total_extracted']:>8.0f} | "
            f"${r['net_profit']:>9.0f} | "
            f"{roi_str:>6} | "
            f"{r['accounts_blown']:>2}/{5} | "
            f"{r['total_payouts_count']:>7} | "
            f"{r['signals']:>7} | "
            f"{r['signal_winrate']*100:>4.1f}%"
        )

    # Print wave stats for C/D
    for r in results:
        if "error" in r:
            continue
        if r.get("scout_trades", 0) > 0 or r.get("confirmer_fills", 0) > 0:
            print(f"\n  Strategy {r['strategy']} ({r['label']}) — Wave breakdown:")
            print(
                f"    Scout:     {r['scout_trades']:>4} trades, "
                f"{r['scout_winrate']*100:>5.1f}% WR, ${r['scout_pnl']:>8.0f}"
            )
            print(
                f"    Confirmer: {r['confirmer_trades']:>4} trades, "
                f"{r['confirmer_winrate']*100:>5.1f}% WR, ${r['confirmer_pnl']:>8.0f} "
                f"({r['confirmer_fills']} fills, {r['confirmer_cancels']} cancels)"
            )
            print(
                f"    Sniper:    {r['sniper_trades']:>4} trades, "
                f"{r['sniper_winrate']*100:>5.1f}% WR, ${r['sniper_pnl']:>8.0f}"
            )
            if r.get("compound_trades", 0) > 0:
                print(
                    f"    Compound:  {r['compound_trades']:>4} taken, "
                    f"{r['compound_wins']} wins"
                )


if __name__ == "__main__":
    main()
