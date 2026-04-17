"""
Approach Feature Computer — computes pre-touch order flow features.

Calculates approach-window features from buffered trades and BBO updates
in the time window BEFORE a level touch. All features use only MBP-1
(top-of-book) data: trade price/size + best bid/ask price/size.

Window: [touch_ts - approach_minutes, touch_ts) — exclusive at touch
time, strictly backward-looking, zero look-ahead bias.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

from alpha_lab.dashboard.pipeline.rithmic_client import BBOUpdate, TradeUpdate

# Match Quant-Lab experiment/features.py constants
LARGE_TRADE_THRESHOLD = 10  # contracts
SUB_WINDOW_FRACTION = 0.5  # last half of window for "recent" metrics


class ApproachFeatureComputer:
    """Computes approach-window features from buffered tick data.

    All 8 features are computable from MBP-1 (top-of-book) data:
    trades (price, size, timestamp) + BBO (bid/ask price/size).
    """

    def compute_features(
        self,
        trades: list[TradeUpdate],
        bbo_updates: list[BBOUpdate],
        approach_start: datetime,
        approach_end: datetime,
    ) -> dict[str, float]:
        """Compute the approach-window features.

        Args:
            trades: Trades in [approach_start, approach_end).
            bbo_updates: BBO updates in [approach_start, approach_end).
            approach_start: Start of approach window (inclusive).
            approach_end: End of approach window = touch timestamp (exclusive).

        Returns:
            Dict with up to 8 feature values. Missing features are NaN.
        """
        window_minutes = (approach_end - approach_start).total_seconds() / 60.0
        sub_boundary = approach_start + (approach_end - approach_start) * SUB_WINDOW_FRACTION

        # ── Trade-based features ──────────────────────────────────
        total_volume = 0
        large_volume = 0
        trade_sizes: list[int] = []
        early_volume = 0
        late_volume = 0

        # For volatility: bucket trades into 1-min windows
        minute_prices: dict[int, list[float]] = defaultdict(list)

        for trade in trades:
            s = trade.size
            p = float(trade.price)
            total_volume += s
            trade_sizes.append(s)

            if s >= LARGE_TRADE_THRESHOLD:
                large_volume += s

            if trade.timestamp < sub_boundary:
                early_volume += s
            else:
                late_volume += s

            # Bucket by minute offset for volatility
            offset_min = int((trade.timestamp - approach_start).total_seconds() // 60)
            minute_prices[offset_min].append(p)

        trade_count = len(trades)

        # app_large_trade_vol_pct
        large_vol_pct = large_volume / total_volume if total_volume > 0 else float("nan")

        # app_trade_count
        app_trade_count = float(trade_count)

        # app_volume_acceleration
        early_minutes = max(1, window_minutes * SUB_WINDOW_FRACTION)
        late_minutes = max(1, window_minutes * (1 - SUB_WINDOW_FRACTION))
        early_rate = early_volume / early_minutes if early_volume > 0 else 0
        late_rate = late_volume / late_minutes if late_volume > 0 else 0
        vol_accel = late_rate / early_rate if early_rate > 0 else float("nan")

        # app_avg_trade_size
        avg_size = sum(trade_sizes) / len(trade_sizes) if trade_sizes else float("nan")

        # ── BBO-based features ────────────────────────────────────
        imbalances: list[float] = []
        spreads: list[float] = []

        for bbo in bbo_updates:
            bid = float(bbo.bid_price)
            ask = float(bbo.ask_price)
            bsz = bbo.bid_size
            asz = bbo.ask_size
            total_sz = bsz + asz
            if total_sz > 0:
                imbalances.append(bsz / total_sz)
            if bid > 0 and ask > 0:
                spreads.append(ask - bid)

        # app_avg_tob_imbalance
        avg_imb = sum(imbalances) / len(imbalances) if imbalances else float("nan")

        # app_max_spread
        max_spread = max(spreads) if spreads else float("nan")

        # ── Volatility features ───────────────────────────────────
        # Compute 1-min close prices for return-based volatility
        sorted_minutes = sorted(minute_prices.keys())
        minute_closes = []
        for m in sorted_minutes:
            if minute_prices[m]:
                minute_closes.append(minute_prices[m][-1])  # last price in minute

        returns: list[float] = []
        for i in range(1, len(minute_closes)):
            if minute_closes[i - 1] > 0:
                ret = (minute_closes[i] - minute_closes[i - 1]) / minute_closes[i - 1]
                if math.isfinite(ret):
                    returns.append(ret)

        # Split returns into full and recent (last half of window)
        half_idx = len(returns) // 2
        recent_returns = returns[half_idx:] if half_idx > 0 else returns

        # app_volatility_recent
        if len(recent_returns) >= 2:
            mean_r = sum(recent_returns) / len(recent_returns)
            var_r = sum((r - mean_r) ** 2 for r in recent_returns) / (len(recent_returns) - 1)
            vol_recent = math.sqrt(var_r)
        else:
            vol_recent = float("nan")

        # app_volatility_ratio
        if len(returns) >= 2:
            mean_all = sum(returns) / len(returns)
            var_all = sum((r - mean_all) ** 2 for r in returns) / (len(returns) - 1)
            vol_full = math.sqrt(var_all)
            vol_ratio = vol_recent / vol_full if vol_full > 0 and math.isfinite(vol_recent) else float("nan")
        else:
            vol_ratio = float("nan")

        return {
            "app_large_trade_vol_pct": large_vol_pct,
            "app_trade_count": app_trade_count,
            "app_volume_acceleration": vol_accel,
            "app_avg_trade_size": avg_size,
            "app_avg_tob_imbalance": avg_imb,
            "app_max_spread": max_spread,
            "app_volatility_recent": vol_recent if math.isfinite(vol_recent) else float("nan"),
            "app_volatility_ratio": vol_ratio if math.isfinite(vol_ratio) else float("nan"),
        }
