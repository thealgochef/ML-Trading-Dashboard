# ML Trading Viability Review (March 28, 2026)

## Executive summary

You already have a **strong engineering foundation** (clean callback pipeline, replay parity, account simulation, strategy batch runners), but the system is **not yet live-trading viable** because research validation is incomplete and a few design constraints likely suppress edge capture.

Most important: your own observed bottlenecks (low signal throughput, confidence filter inversion, one-active-observation limitation) are all visible in code paths that directly affect expected value and payout probability.

## What is already strong and relevant

1. **Single event-driven pipeline architecture** with deterministic flow from tick → touch → observation → prediction → execution. This is exactly the right structure for reproducible strategy research and deployment hardening.
2. **Historical replay and standalone batch scripts** already exist for high-speed comparative testing, which is the right substrate for fast iteration on policy logic.
3. **Explicit prop-firm economics layer** (`EconomicConfig`, `EconomicTracker`, payout logic) means you can optimize to the real objective (payout survivability), not just raw PnL.
4. **Regime/Wave scaffolding is built**, so experimentation on execution policy is cheap once model diagnostics are trustworthy.

## Highest-risk gaps (blockers to live viability)

### 1) Research observability gap: no full prediction analytics pipeline yet
You have model inference and outcome tracking, but no productionized analytics pass for:
- calibration reliability,
- level/session conditional EV,
- confidence monotonicity,
- MFE/MAE decomposition by segment.

Without this, policy knobs (confidence gates, session filters, wave rules) are mostly heuristic.

### 2) Single active observation window drops valid opportunities
`ObservationManager` currently permits only one active observation; overlapping touches are dropped. This likely explains part of your low executable-signal rate and weakens compounding feasibility.

### 3) Confidence gating logic appears misaligned with realized win rate
RegimeWave sniper/harvest filters still use fixed confidence thresholds even though your results indicate confidence may be inversely informative for win rate in current data slices.

### 4) Label/decision mismatch risk between model objective and execution objective
Prediction classes are mapped from MFE/MAE thresholds in `OutcomeTracker`, while execution objective is payout/survival under Apex constraints. These are related but not equivalent; there is no explicit utility-aware threshold optimization layer in between.

### 5) Limited robustness controls for distribution shift
Execution is currently locked to RTH (correct for now), but there is no visible ongoing drift monitor (feature distribution drift, class prior drift, calibration drift) to protect live performance over time.

## Priority roadmap (what to do next, in order)

## Phase 1 (1-2 weeks): Build analytics truth layer before strategy changes

### A. Implement full prediction analytics dataset export
Create one canonical per-prediction table including:
- metadata: date/session/level type/zone id,
- model outputs: class probs, predicted class,
- execution flags and whether a trade was taken,
- realized outcomes: MFE/MAE, resolution type, realized trade PnL if traded.

### B. Run mandatory diagnostics
- Reliability/calibration curves for `tradeable_reversal`.
- Win rate and EV by confidence deciles.
- EV by level type (PDH/PDL/Asia/London/etc.) and by session-time buckets.
- Conditional MFE/MAE distributions for trades taken vs skipped.

### C. Define go/no-go policy tests
Before any policy rollout, require:
- minimum sample count per segment,
- lower confidence bound of EV > 0 for enabled segments,
- max drawdown/ruin probability under bootstrap stress.

## Phase 2 (1-2 weeks): Increase signal throughput safely

### A. Replace single active observation with multi-window queue
Refactor `ObservationManager` to manage multiple concurrent windows keyed by `event_id` (or zone_id + touch timestamp), each with independent lifecycle and completion callbacks.

### B. Add anti-duplication policy to avoid overtrading
To avoid correlated overexposure after enabling multi-window:
- max one open position per account (already present),
- optional global cooldown (e.g., 1-2 min) if two same-direction signals fire back-to-back,
- optional per-level-type daily cap.

### C. Re-estimate signal statistics
After multi-window support, rerun baseline 15/30 mirror and recompute:
- signals/day,
- win rate shift,
- payout probability,
- average time to payout threshold.

## Phase 3 (1 week): Replace fixed confidence rules with calibrated decision policy

### A. Stop using raw probability thresholds directly
Fit a calibration layer (isotonic or Platt) on out-of-sample predictions.

### B. Optimize policy on expected utility, not class probability
For each candidate trade compute:
`expected_utility = p(win)*win_value - p(loss)*loss_value - friction - drawdown_penalty`
where win/loss value depends on regime and Apex state (distance to safety net, DLL remaining, payout eligibility).

### C. Convert sniper/harvest from static thresholds to utility thresholds
- Sniper enters when expected utility > 0 and risk budget available.
- Harvest uses stricter utility threshold + daily cap.

## Phase 4 (ongoing): Hardening for live deployment

1. **Walk-forward protocol**: rolling retrain / rolling test windows with locked hyperparameters.
2. **Drift monitors**: PSI/KS on each feature and calibration drift alarms.
3. **Kill-switches**:
   - auto-disable under consecutive loss streak,
   - auto-disable if realized win rate falls below lower CI threshold,
   - session/level-type auto-disable on negative EV breach.
4. **Operational reproducibility**:
   - pin Python/toolchain consistently across docs and `pyproject` metadata,
   - store model artifact + feature schema hash + training date in version metadata.

## Concrete code-level opportunities

1. `ObservationManager`: move from singleton `_active` to `dict[event_id, ObservationWindow]` with per-window end checks.
2. `PredictionEngine`: attach calibrated probability + policy score fields in emitted prediction payload.
3. `OutcomeTracker`: persist every resolution into analytics store (CSV/Parquet/DB) for offline analysis.
4. `RegimeWaveExecutor`:
   - replace `_DEFAULT_SNIPER_CONFIDENCE` gating with pluggable decision rule,
   - log per-decision reason codes (`entered`, `filtered_conf`, `filtered_regime`, `filtered_risk`).
5. Batch scripts:
   - add bootstrap confidence intervals on payout probability,
   - output per-segment performance tables (session, level type, confidence bin).

## Viability criteria checklist (what "ready for live" should mean)

You are ready for constrained live rollout only if all are true:
- 6+ months walk-forward with no catastrophic degradation.
- Positive EV after costs in at least two non-overlapping periods.
- Payout probability remains above your minimum target under bootstrap stress.
- Drift alarms and kill-switches tested in replay fault injection.
- Decision policy demonstrates monotonic utility ranking (not just raw confidence ranking).

## Bottom line

The project is **close to viability as a research-to-execution framework**, but **not yet a live-trading ML product**. The fastest path is:
1) build the missing analytics truth layer,
2) remove dropped-signal bottlenecks,
3) switch from static confidence heuristics to calibrated utility-based execution,
4) enforce walk-forward + drift + kill-switch governance.

If you do only one thing next: ship the prediction analytics layer first. Everything else should be downstream of those results.
