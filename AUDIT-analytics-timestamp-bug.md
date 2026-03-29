# Analytics TP/SL Timestamp Bug — Audit Findings

## Context

We identified a **52.1% vs 73.2% win rate discrepancy** between `run_prediction_analytics.py` and `run_strategy_comparison.py` (Strategy B). Two theories were proposed:

1. **Claude's theory (rejected):** Entry price mismatch between scripts — sweep enters near level price, analytics enters at displaced market price.
2. **GPT's theory (confirmed):** Timestamp bug in analytics TP/SL path construction — simulation starts from touch time instead of prediction-fire time.

A prior audit also identified a **secondary contributor**: `is_executable` ≠ "trade actually taken" (account state gating). Both factors are verified below.

---

## Finding 1: All Scripts Use the Same Entry Price Convention

**Verdict: Claude's entry-price-mismatch theory is NOT supported by the code.**

All three scripts determine entry price identically:

| Script | Entry Price Logic | File:Line |
|--------|------------------|-----------|
| `run_tp_sl_sweep.py` | `state["latest_price"]` if available, else `prediction.level_price` | `run_tp_sl_sweep.py:304-308` |
| `run_strategy_comparison.py` | `state["latest_price"]` if available, else `prediction.level_price` | `run_strategy_comparison.py:327-331` |
| `run_prediction_analytics.py` | `state["latest_price"]` if available, else `prediction.level_price` | `run_prediction_analytics.py:472-477` |

`TradeExecutor.on_prediction()` confirms:
```python
# trade_executor.py:70
entry_price: Decimal = current_price if current_price is not None else prediction["level_price"]
```
Docstring: *"Entry is at current_price (market price when prediction fires), NOT at the level/touch price from 5 minutes earlier."*

---

## Finding 2: `Prediction.timestamp` Is Touch Time, Not Prediction-Fire Time

**Verified at `prediction_engine.py:85`:**
```python
prediction = Prediction(
    timestamp=observation.event.timestamp,  # ← touch time (t0)
    ...
)
```

The observation window stores the actual end time separately:
```python
# observation_manager.py:82-83
window = ObservationWindow(
    start_time=event.timestamp,                        # t0
    end_time=event.timestamp + OBSERVATION_DURATION,   # t0 + 5 minutes
)
```

`prediction.observation.end_time` exists and equals `t0 + 5min`, but `prediction.timestamp` is `t0` (the touch).

---

## Finding 3: Analytics TP/SL Simulation Starts From Wrong Time

**This is the bug. Verified at `run_prediction_analytics.py:597-598`:**

```python
end_ts = _session_end_for_prediction(pred.timestamp)        # session end from touch time
path = [px for ts, px in ticks if pred.timestamp <= ts <= end_ts]  # path starts at TOUCH TIME
```

The `evaluate_traded_outcome()` function (line 628-634) then walks this path tick-by-tick:
```python
evaluate_traded_outcome(
    direction=pred.trade_direction,
    entry_price=pred.entry_price_at_prediction,  # price at ~t0+5m (correct)
    trade_path_prices=path,                       # ticks from t0 onward (WRONG)
    tp_points=tp, sl_points=sl,
)
```

**The inconsistency:**
- `entry_price` = market price at prediction-fire time (~t0+5m) ✓
- `trade_path_prices` = ticks starting from touch time (t0) ✗

This feeds ~5 minutes of pre-entry ticks into TP/SL evaluation. Those ticks occurred before the trade could possibly exist.

---

## Finding 4: Strategy Runners Are Forward-Only (Correct Behavior)

### Live trading flow (server.py)
The entire pipeline is synchronous within one tick:
```
t0+5m+ε:  Tick arrives with price Pε
           ├─ state.latest_price = Pε                    (server.py:549)
           ├─ observation_manager.on_trade() detects      (obs_manager.py:102)
           │   trade.timestamp > window.end_time
           ├─ _complete_window() fires synchronously      (obs_manager.py:185-210)
           ├─ prediction_engine.predict() runs            (prediction_engine.py:37-99)
           ├─ TradeExecutor.on_prediction(price=Pε)       (trade_executor.py:44-98)
           └─ Position opens at Pε
```

### Replay flow (run_tp_sl_sweep.py / run_strategy_comparison.py)
Identical synchronous chain:
```
_on_trade(trade):
  state["latest_price"] = price              (line 457 / 504)
  touch_detector.on_trade(trade)             (line 480 / 534)
  observation_manager.on_trade(trade)        (line 485 / 539)
    → if timestamp > end_time → completes
      → prediction fires synchronously
        → _on_prediction() reads state["latest_price"]
          → trade_executor enters at that price
```

**PositionMonitor** (`position_monitor.py:66-159`) processes each tick's price against open positions. It only sees ticks that arrive AFTER the position is opened. No retroactive path reconstruction.

**ReplayClient** (`replay_client.py:231-361`) delivers ticks one-at-a-time, synchronously. Each tick is fully processed (including any position opens) before the next tick is loaded.

---

## Finding 5: Why This Makes Analytics Artificially Pessimistic

Example scenario:
```
t0:        Touch at PDH (21,500). Level price = 21,500.
t0→t0+5m:  Price reverses down during observation window.
t0+5m+ε:   Prediction fires. Market price = 21,496 (entry price for SHORT).
           TP = 21,496 - 8 = 21,488.  SL = 21,496 + 5 = 21,501.
```

**Analytics path** (buggy): starts at t0. Includes ticks at 21,500, 21,501, 21,502 from the first minute near the level. These prices are above the SL threshold (21,501), so the simulation records a stop-loss hit — **on price action that occurred before the trade existed**.

**Strategy runner path** (correct): starts at t0+5m+ε. Only sees ticks from 21,496 onward. The trade may never hit 21,501 again, resulting in a TP hit.

---

## Finding 6: `is_executable` ≠ "Trade Actually Taken" (Secondary Factor)

`is_executable` is set purely by prediction quality + session (`prediction_engine.py:66-69`):
```python
is_executable = (
    predicted_class == "tradeable_reversal"
    and observation.event.session == "ny_rth"
)
```

It does NOT check account state. Execution-time gating in `TradeExecutor.on_prediction()` adds:

| Gate | Location | Effect |
|------|----------|--------|
| `is_executable` check | `trade_executor.py:56-58` | Rejects non-executable |
| Flatten time (≥3:55 PM ET) | `trade_executor.py:60-66` | Blocks late trades |
| Conflicting position (no-hedge) | `trade_executor.py:72-82` | Blocks or flips |
| `get_tradeable_accounts()` | `account_manager.py:59-67` | Requires ACTIVE + no open position |

The analytics script evaluates every executable prediction independently. The strategy runners skip executable signals when accounts are already in a trade. This means the analytics "executable" slice is a **superset** of actually-entered trades.

---

## Root Cause Summary

| Factor | Impact | Confidence |
|--------|--------|------------|
| **Timestamp bug** — analytics TP/SL path starts at touch time (t0) instead of prediction-fire time (~t0+5m) | **Primary** — includes ~5 min of pre-entry ticks in TP/SL evaluation, causing false stop-outs | High |
| **Gating gap** — analytics evaluates all executable signals; strategy runners skip signals blocked by account state | **Secondary** — analytics includes trades the strategy would never have taken | High |
| **Entry price mismatch** (Claude's theory) — sweep enters at level price, analytics at displaced price | **Not a factor** — all scripts use identical market-price-at-prediction-fire logic | Disproven |

---

## Fix

### The Problem
`run_prediction_analytics.py:597-598` constructs the TP/SL simulation path starting from `pred.timestamp` (touch time), but uses an entry price from ~5 minutes later (prediction-fire time). This includes pre-entry ticks in the simulation.

### Why NOT `observation.end_time`
An earlier draft proposed using `prediction.observation.end_time` (touch + 5min exactly). This is close but not precise:
- `observation.end_time` is a synthetic boundary — no tick may exist at exactly that time
- The observation completes on the **first tick whose timestamp exceeds** `end_time` (`observation_manager.py:102`)
- Ticks between `end_time` and the actual completing tick would have already been processed before the position opened in live trading

### The Correct Solution
Capture the **actual tick timestamp** when `_on_prediction` fires — the same tick whose price is used as `entry_price`. This is the real entry moment in both live and replay.

```python
# Track the completing tick's timestamp in state
state["latest_tick_ts"] = trade.timestamp  # set in _on_trade, before pipeline

# Capture it when prediction fires
entry_timestamp = state["latest_tick_ts"]  # the tick that triggered completion

# Use it as path start
path = [px for ts, px in ticks if pred.entry_timestamp <= ts <= end_ts]
```

This matches exactly what happens in live trading: the trade enters on the tick that completes the observation window, and TP/SL is evaluated only on ticks from that moment forward.

---

## Fix 2: Strategy B Position State Simulation

### The Problem
The analytics script evaluates every executable prediction independently. In the actual Strategy B runner, `TradeExecutor` skips executable signals when:
- The account already holds a position (`get_tradeable_accounts()` requires `not a.has_position`)
- A conflicting-direction position is open (`second_signal_mode="ignore"`)
- The signal arrives at/after flatten time (3:55 PM ET)

This means the "executable" slice in analytics is a **superset** of trades Strategy B actually takes.

### The Solution
Simulate a single virtual position (matching Strategy B's TP=15/SL=30, 5 intraday accounts, ignore mode) in the analytics prediction loop. For each executable prediction in chronological order:
1. Check flatten-time gate (3:55 PM ET)
2. Check if a simulated position is still open from a prior signal
3. If both clear, mark as `simulated_trade_taken=True` and track the position's exit timestamp
4. Otherwise, mark as blocked with a specific reason

New CSV fields:
- `simulated_trade_taken` — `True` if Strategy B would have entered this trade
- `simulated_blocked_reason` — `""`, `"not_executable"`, `"flatten_time"`, `"position_open"`, or `"conflicting_position"`

This lets you filter the analytics output to the exact slice that matches Strategy B's actual trade ledger.
