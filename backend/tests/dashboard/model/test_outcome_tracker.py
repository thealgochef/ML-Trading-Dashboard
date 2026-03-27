"""
Phase 3 — Outcome Tracker Tests

Tests prediction outcome resolution based on price movement after
signals. The outcome tracker determines if predictions were correct
by monitoring MFE and MAE thresholds.

Business context: Outcome tracking enables the resolved prediction
markers on the chart (checkmark or X border) and feeds the performance
analytics in the Analysis tab. Accurate outcome tracking is essential
for evaluating model performance in live conditions.
"""

from __future__ import annotations

from alpha_lab.dashboard.engine.models import TradeDirection
from alpha_lab.dashboard.model import ResolvedOutcome
from alpha_lab.dashboard.model.outcome_tracker import OutcomeTracker

from .conftest import make_prediction, make_trade

# ── Tests ────────────────────────────────────────────────────────


def test_mfe_tracking_long():
    """LONG prediction correctly tracks max favorable excursion (price above level)."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        direction=TradeDirection.LONG, level_price=20100.00,
    )
    tracker.start_tracking(pred)

    # Trades above level accumulate MFE
    tracker.on_trade(make_trade(ts_offset_s=10, price=20105.00))  # MFE=5
    tracker.on_trade(make_trade(ts_offset_s=20, price=20115.00))  # MFE=15
    tracker.on_trade(make_trade(ts_offset_s=30, price=20125.00))  # MFE=25 → TP hit

    # Should resolve at MFE >= 25
    assert tracker.active_trackers == 0


def test_mfe_tracking_short():
    """SHORT prediction correctly tracks max favorable (price below level)."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        direction=TradeDirection.SHORT, level_price=20100.00,
    )
    tracker.start_tracking(pred)

    # Trades below level accumulate MFE for SHORT
    tracker.on_trade(make_trade(ts_offset_s=10, price=20095.00))  # MFE=5
    tracker.on_trade(make_trade(ts_offset_s=20, price=20085.00))  # MFE=15
    outcomes = tracker.on_trade(make_trade(ts_offset_s=30, price=20075.00))  # MFE=25

    assert len(outcomes) == 1
    assert outcomes[0].actual_class == "tradeable_reversal"
    assert outcomes[0].resolution_type == "tp_hit"
    assert outcomes[0].mfe_points >= 25.0


def test_mae_tracking():
    """Correctly tracks max adverse excursion."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        direction=TradeDirection.LONG, level_price=20100.00,
    )
    tracker.start_tracking(pred)

    # Trades below level accumulate MAE for LONG
    tracker.on_trade(make_trade(ts_offset_s=10, price=20095.00))  # MAE=5
    tracker.on_trade(make_trade(ts_offset_s=20, price=20080.00))  # MAE=20

    # Not yet resolved (MAE < 37.5)
    assert tracker.active_trackers == 1

    # Hit stop level
    outcomes = tracker.on_trade(
        make_trade(ts_offset_s=30, price=20062.50),  # MAE=37.5
    )

    assert len(outcomes) == 1
    assert outcomes[0].resolution_type == "sl_hit"
    assert outcomes[0].mae_points >= 37.5


def test_both_thresholds_same_tick_mfe_wins():
    """When a single tick crosses both MFE and MAE thresholds, MFE wins."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        predicted_class="tradeable_reversal",
        direction=TradeDirection.LONG,
        level_price=20100.00,
    )
    tracker.start_tracking(pred)

    # First: build up MAE close to threshold
    tracker.on_trade(make_trade(ts_offset_s=5, price=20063.00))  # MAE=37

    # Then: a huge favorable spike that crosses BOTH thresholds simultaneously
    # MFE jumps from 0 to 25+ AND MAE was already at 37 from prior trade
    outcomes = tracker.on_trade(make_trade(ts_offset_s=10, price=20125.00))

    # MFE checked first → tp_hit wins
    assert len(outcomes) == 1
    assert outcomes[0].resolution_type == "tp_hit"
    assert outcomes[0].actual_class == "tradeable_reversal"
    assert outcomes[0].prediction_correct is True
    assert outcomes[0].mfe_points >= 25.0
    assert outcomes[0].mae_points >= 37.0


def test_resolve_reversal_on_tp():
    """MFE >= 25 points resolves as tradeable_reversal with tp_hit."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        direction=TradeDirection.LONG, level_price=20100.00,
    )
    tracker.start_tracking(pred)

    outcomes = tracker.on_trade(make_trade(ts_offset_s=10, price=20125.00))

    assert len(outcomes) == 1
    assert outcomes[0].actual_class == "tradeable_reversal"
    assert outcomes[0].resolution_type == "tp_hit"
    assert outcomes[0].mfe_points == 25.0


def test_resolve_blowthrough_on_sl():
    """MAE >= 37.5 with MFE < 5 resolves as aggressive_blowthrough."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        direction=TradeDirection.LONG, level_price=20100.00,
    )
    tracker.start_tracking(pred)

    # Small favorable move (MFE=1, below TRAP_MFE_MIN=5)
    tracker.on_trade(make_trade(ts_offset_s=10, price=20101.00))
    # Big adverse move (MAE=37.5)
    outcomes = tracker.on_trade(make_trade(ts_offset_s=20, price=20062.50))

    assert len(outcomes) == 1
    assert outcomes[0].actual_class == "aggressive_blowthrough"
    assert outcomes[0].resolution_type == "sl_hit"
    assert outcomes[0].mfe_points < 5.0


def test_resolve_trap_on_sl():
    """MAE >= 37.5 with MFE >= 5 resolves as trap_reversal."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        direction=TradeDirection.LONG, level_price=20100.00,
    )
    tracker.start_tracking(pred)

    # Decent favorable move first (MFE=10)
    tracker.on_trade(make_trade(ts_offset_s=10, price=20110.00))
    # Then big adverse move (MAE=37.5)
    outcomes = tracker.on_trade(make_trade(ts_offset_s=20, price=20062.50))

    assert len(outcomes) == 1
    assert outcomes[0].actual_class == "trap_reversal"
    assert outcomes[0].resolution_type == "sl_hit"
    assert outcomes[0].mfe_points >= 5.0


def test_prediction_correct_true():
    """Predicted reversal that resolves as reversal is prediction_correct=True."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        predicted_class="tradeable_reversal",
        direction=TradeDirection.LONG,
        level_price=20100.00,
    )
    tracker.start_tracking(pred)

    outcomes = tracker.on_trade(make_trade(ts_offset_s=10, price=20125.00))

    assert outcomes[0].prediction_correct is True


def test_prediction_correct_false():
    """Predicted reversal that resolves as blowthrough is prediction_correct=False."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        predicted_class="tradeable_reversal",
        direction=TradeDirection.LONG,
        level_price=20100.00,
    )
    tracker.start_tracking(pred)

    # Blowthrough: tiny MFE then big MAE
    tracker.on_trade(make_trade(ts_offset_s=10, price=20101.00))
    outcomes = tracker.on_trade(make_trade(ts_offset_s=20, price=20062.50))

    assert outcomes[0].prediction_correct is False
    assert outcomes[0].actual_class == "aggressive_blowthrough"


def test_session_end_resolves_all():
    """Unresolved predictions resolve at session end."""
    tracker = OutcomeTracker()

    # Use levels close together so trades stay within both thresholds
    pred1 = make_prediction(
        event_id="evt_1", level_price=20100.00,
        direction=TradeDirection.LONG,
    )
    pred2 = make_prediction(
        event_id="evt_2", level_price=20110.00,
        direction=TradeDirection.LONG,
    )
    tracker.start_tracking(pred1)
    tracker.start_tracking(pred2)

    # Trade near both levels — small MFE for pred1, small MAE for pred2
    # pred1 LONG@20100: MFE=5, MAE=0
    # pred2 LONG@20110: MFE=0, MAE=5 (well below 37.5)
    tracker.on_trade(make_trade(ts_offset_s=10, price=20105.00))

    assert tracker.active_trackers == 2

    outcomes = tracker.on_session_end()

    assert len(outcomes) == 2
    assert all(o.resolution_type == "session_end" for o in outcomes)
    assert tracker.active_trackers == 0


def test_resolved_outcome_fields():
    """ResolvedOutcome has all required fields for DB update."""
    tracker = OutcomeTracker()
    pred = make_prediction(
        predicted_class="tradeable_reversal",
        direction=TradeDirection.LONG,
        level_price=20100.00,
    )
    tracker.start_tracking(pred)

    outcomes = tracker.on_trade(make_trade(ts_offset_s=10, price=20125.00))
    outcome = outcomes[0]

    assert isinstance(outcome, ResolvedOutcome)
    assert isinstance(outcome.event_id, str)
    assert isinstance(outcome.mfe_points, float)
    assert isinstance(outcome.mae_points, float)
    assert isinstance(outcome.resolution_type, str)
    assert isinstance(outcome.prediction_correct, bool)
    assert isinstance(outcome.actual_class, str)
    assert outcome.resolved_at is not None


def test_callback_fires_on_resolve():
    """Registered callback receives ResolvedOutcome."""
    tracker = OutcomeTracker()
    received: list[ResolvedOutcome] = []
    tracker.on_outcome_resolved(lambda o: received.append(o))

    pred = make_prediction(
        direction=TradeDirection.LONG, level_price=20100.00,
    )
    tracker.start_tracking(pred)

    tracker.on_trade(make_trade(ts_offset_s=10, price=20125.00))

    assert len(received) == 1
    assert received[0].event_id == pred.event_id


def test_multiple_concurrent_trackers():
    """Multiple predictions tracked independently."""
    tracker = OutcomeTracker()

    # A: LONG@20100, B: SHORT@20130 (close levels, different directions)
    pred_a = make_prediction(
        event_id="evt_a",
        direction=TradeDirection.LONG,
        level_price=20100.00,
    )
    pred_b = make_prediction(
        event_id="evt_b",
        direction=TradeDirection.SHORT,
        level_price=20130.00,
    )
    tracker.start_tracking(pred_a)
    tracker.start_tracking(pred_b)

    assert tracker.active_trackers == 2

    # Trade at 20125: A gets MFE=25 (resolves), B gets favorable=5 (no resolve)
    outcomes_1 = tracker.on_trade(make_trade(ts_offset_s=10, price=20125.00))

    assert len(outcomes_1) == 1
    assert outcomes_1[0].event_id == "evt_a"
    assert tracker.active_trackers == 1

    # Trade at 20105: B gets favorable=25 (resolves)
    outcomes_2 = tracker.on_trade(make_trade(ts_offset_s=20, price=20105.00))

    assert len(outcomes_2) == 1
    assert outcomes_2[0].event_id == "evt_b"
    assert tracker.active_trackers == 0
