"""
Data routes — historical data queries.

GET /api/data/trades
GET /api/data/predictions
GET /api/data/performance
GET /api/data/equity-curve
GET /api/data/ohlcv
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/trades")
async def get_trades(request: Request) -> dict:
    state = request.app.state.dashboard
    return {"trades": list(state.todays_trades)}


@router.get("/predictions")
async def get_predictions(request: Request) -> dict:
    state = request.app.state.dashboard
    return {"predictions": list(state.todays_predictions)}


@router.get("/performance")
async def get_performance(request: Request) -> dict:
    state = request.app.state.dashboard

    trades = state.todays_trades
    total_trades = len(trades)
    wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
    losses = sum(1 for t in trades if float(t.get("pnl", 0)) < 0)
    total_pnl = sum(float(t.get("pnl", 0)) for t in trades)

    predictions = state.todays_predictions
    pred_correct = sum(1 for p in predictions if p.get("prediction_correct"))
    pred_total = sum(
        1 for p in predictions
        if p.get("prediction_correct") is not None
    )

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
        "win_rate": round(wins / total_trades, 4) if total_trades > 0 else 0,
        "prediction_accuracy": round(pred_correct / pred_total, 4) if pred_total > 0 else 0,
    }


@router.get("/equity-curve")
async def get_equity_curve(request: Request, account_id: str | None = None) -> dict:
    state = request.app.state.dashboard

    # Return time-series snapshots (initial + after each trade close)
    snapshots = list(state.equity_snapshots)
    if account_id:
        snapshots = [s for s in snapshots if s["account_id"] == account_id]

    # Append current state as the latest data point
    for acct in state.account_manager.get_all_accounts():
        if account_id and acct.account_id != account_id:
            continue
        snapshots.append({
            "timestamp": "now",
            "account_id": acct.account_id,
            "balance": float(acct.balance),
            "profit": float(acct.profit),
            "group": acct.group,
            "tier": acct.tier,
            "status": acct.status.value,
        })

    return {"snapshots": snapshots}


@router.get("/ohlcv")
async def get_ohlcv(
    request: Request,
    timeframe: str = "1m",
    since: str | None = None,
) -> dict:
    state = request.app.state.dashboard

    # If pipeline is wired, pull OHLCV from the price buffer
    if state.pipeline is not None:
        from datetime import datetime, timedelta, timezone

        if since:
            since_dt = datetime.fromisoformat(since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        elif getattr(state, "replay_mode", False):
            # Replay mode: data is historical, wall clock is irrelevant.
            # Return all available bars.
            since_dt = datetime.min.replace(tzinfo=timezone.utc)
        else:
            # Live mode: last 7 days (includes historical backfill)
            since_dt = datetime.now(timezone.utc) - timedelta(days=7)

        # Prefer TickBarBuilder's stored bars (survives regardless of
        # client connection timing) over PriceBuffer's deque-based rebuild.
        builder = getattr(state, "tick_bar_builder", None)
        if builder is not None and timeframe in ("987t", "2000t"):
            stored = builder.get_bars(timeframe, include_partial=True)
            if since:
                # Explicit since: filter as requested
                bars = [b for b in stored if b.timestamp >= since_dt]
            else:
                # No explicit since: return ALL stored bars (includes
                # preloaded historical data that may be older than 7d).
                bars = stored
        else:
            bars = state.pipeline._buffer.get_ohlcv(timeframe, since_dt)

        return {
            "bars": [
                {
                    "timestamp": bar.timestamp.isoformat(),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": bar.volume,
                }
                for bar in bars
            ],
            "timeframe": timeframe,
        }

    return {"bars": [], "timeframe": timeframe}


@router.get("/debug/pipeline")
async def debug_pipeline(request: Request) -> dict:
    """Temporary debug endpoint — remove after debugging."""
    state = request.app.state.dashboard
    buffer = state.pipeline._buffer if state.pipeline else None

    tick_bar_builder = getattr(state, "tick_bar_builder", None)

    result: dict = {
        "pipeline_exists": state.pipeline is not None,
        "trades_in_deque": len(buffer._trades) if buffer else "no buffer",
        "historical_bars_count": len(buffer._historical_bars) if buffer else "no buffer",
    }

    if tick_bar_builder is not None:
        result["tick_bar_builder_accumulators"] = {
            tf: {"count": acc.count, "tick_count": acc.tick_count}
            for tf, acc in tick_bar_builder._accumulators.items()
        }
        if hasattr(tick_bar_builder, "_completed_bars"):
            result["completed_bars"] = {
                tf: len(bars)
                for tf, bars in tick_bar_builder._completed_bars.items()
            }
            # Show first and last bar of 987t if any
            bars_987 = tick_bar_builder._completed_bars.get("987t", [])
            if bars_987:
                first = bars_987[0]
                last = bars_987[-1]
                result["first_987t_bar"] = {
                    "timestamp": str(first.timestamp),
                    "open": float(first.open),
                    "high": float(first.high),
                    "low": float(first.low),
                    "close": float(first.close),
                    "volume": first.volume,
                }
                result["last_987t_bar"] = {
                    "timestamp": str(last.timestamp),
                    "open": float(last.open),
                    "high": float(last.high),
                    "low": float(last.low),
                    "close": float(last.close),
                    "volume": last.volume,
                }
        else:
            result["completed_bars"] = "no _completed_bars attr"
    else:
        result["tick_bar_builder"] = "not wired"

    return result
