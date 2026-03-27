"""
Phase 2 — Observation Window Manager Tests

Tests the 5-minute observation window lifecycle from open to close,
including edge cases like feed drops and manual level deletion.

Business context: The observation window accumulates tick data that
the feature computer uses. Incomplete windows (feed drops) produce
unreliable features and must be discarded. The model was trained on
complete 5-minute windows only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_lab.dashboard.engine.feature_computer import FeatureComputer
from alpha_lab.dashboard.engine.models import (
    LevelSide,
    LevelZone,
    ObservationStatus,
    ObservationWindow,
    TouchEvent,
    TradeDirection,
)
from alpha_lab.dashboard.engine.observation_manager import ObservationManager
from alpha_lab.dashboard.pipeline.rithmic_client import (
    BBOUpdate,
    ConnectionStatus,
    TradeUpdate,
)

# ── Helpers ──────────────────────────────────────────────────────

BASE_TS = datetime(2026, 3, 2, 14, 30, 0, tzinfo=UTC)


def _trade(
    ts_offset_s: float = 0,
    price: float = 20100.00,
    size: int = 5,
) -> TradeUpdate:
    ts = BASE_TS + timedelta(seconds=ts_offset_s)
    return TradeUpdate(
        timestamp=ts,
        price=Decimal(str(price)),
        size=size,
        aggressor_side="BUY",
        symbol="NQH6",
    )


def _bbo(
    ts_offset_s: float = 0,
    bid: float = 20099.75,
    ask: float = 20100.25,
) -> BBOUpdate:
    ts = BASE_TS + timedelta(seconds=ts_offset_s)
    return BBOUpdate(
        timestamp=ts,
        bid_price=Decimal(str(bid)),
        bid_size=15,
        ask_price=Decimal(str(ask)),
        ask_size=12,
        symbol="NQH6",
    )


def _make_touch_event(
    ts: datetime = BASE_TS,
    price: float = 20100.00,
    direction: TradeDirection = TradeDirection.LONG,
    level_price: float = 20100.00,
) -> TouchEvent:
    zone = LevelZone(
        zone_id="test_zone",
        representative_price=Decimal(str(level_price)),
        side=LevelSide.LOW,
    )
    return TouchEvent(
        event_id="test_event_1",
        timestamp=ts,
        level_zone=zone,
        trade_direction=direction,
        price_at_touch=Decimal(str(price)),
        session="ny_rth",
    )


# ── Tests ────────────────────────────────────────────────────────


def test_start_observation_opens_window():
    """After start, active_observation is set."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    event = _make_touch_event()
    window = mgr.start_observation(event)

    assert window is not None
    assert mgr.active_observation is not None
    assert window.status == ObservationStatus.ACTIVE
    assert window.start_time == BASE_TS
    assert window.end_time == BASE_TS + timedelta(minutes=5)


def test_trades_accumulated_during_window():
    """Trades added between start and end appear in window."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    event = _make_touch_event()
    mgr.start_observation(event)

    mgr.on_trade(_trade(ts_offset_s=10, price=20100.25))
    mgr.on_trade(_trade(ts_offset_s=30, price=20099.75))
    mgr.on_trade(_trade(ts_offset_s=60, price=20101.00))

    assert len(mgr.active_observation.trades_accumulated) == 3


def test_window_completes_after_5_minutes():
    """Callback fires with completed observation after 5 min."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    completed: list[ObservationWindow] = []
    mgr.on_observation_complete(lambda w: completed.append(w))

    event = _make_touch_event()
    mgr.start_observation(event)

    # Add some trades
    mgr.on_bbo(_bbo(ts_offset_s=0))
    mgr.on_trade(_trade(ts_offset_s=10))

    # Send a trade AFTER 5 minutes to trigger completion
    mgr.on_trade(_trade(ts_offset_s=301, price=20102.00))

    assert len(completed) == 1
    assert completed[0].status == ObservationStatus.COMPLETED
    assert mgr.active_observation is None


def test_features_computed_on_completion():
    """Completed observation has non-None features dict."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    completed: list[ObservationWindow] = []
    mgr.on_observation_complete(lambda w: completed.append(w))

    event = _make_touch_event()
    mgr.start_observation(event)

    mgr.on_bbo(_bbo(ts_offset_s=0))
    mgr.on_trade(_trade(ts_offset_s=10))
    mgr.on_trade(_trade(ts_offset_s=301))  # triggers completion

    assert completed[0].features is not None
    assert "int_time_beyond_level" in completed[0].features
    assert "int_time_within_2pts" in completed[0].features
    assert "int_absorption_ratio" in completed[0].features


def test_feed_drop_discards_window():
    """Connection status change to DISCONNECTED/RECONNECTING discards active window."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    completed: list[ObservationWindow] = []
    mgr.on_observation_complete(lambda w: completed.append(w))

    event = _make_touch_event()
    mgr.start_observation(event)

    mgr.on_trade(_trade(ts_offset_s=10))

    # Simulate feed drop
    mgr.on_connection_status(ConnectionStatus.RECONNECTING)

    assert mgr.active_observation is None
    assert len(completed) == 1
    assert completed[0].status == ObservationStatus.DISCARDED_FEED_DROP


def test_discarded_window_flagged():
    """Discarded window has status DISCARDED_FEED_DROP."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    completed: list[ObservationWindow] = []
    mgr.on_observation_complete(lambda w: completed.append(w))

    event = _make_touch_event()
    mgr.start_observation(event)

    mgr.on_connection_status(ConnectionStatus.RECONNECTING)

    assert completed[0].status == ObservationStatus.DISCARDED_FEED_DROP
    assert completed[0].features is None


def test_level_deletion_discards_window():
    """Deleting the observed level mid-window discards it."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    completed: list[ObservationWindow] = []
    mgr.on_observation_complete(lambda w: completed.append(w))

    event = _make_touch_event(level_price=20100.00)
    mgr.start_observation(event)

    mgr.on_level_deleted(Decimal("20100.00"))

    assert mgr.active_observation is None
    assert len(completed) == 1
    assert completed[0].status == ObservationStatus.DISCARDED_LEVEL_DELETED


def test_only_one_active_window():
    """Starting a second observation while one is active is rejected."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    event1 = _make_touch_event()
    event2 = _make_touch_event(
        ts=BASE_TS + timedelta(minutes=1),
        price=20200.00,
    )
    event2.event_id = "test_event_2"

    window1 = mgr.start_observation(event1)
    window2 = mgr.start_observation(event2)

    assert window1 is not None
    assert window2 is None
    assert mgr.active_observation.event.event_id == "test_event_1"


def test_no_trades_after_window_close():
    """Trades arriving after 5 minutes don't accumulate."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    completed: list[ObservationWindow] = []
    mgr.on_observation_complete(lambda w: completed.append(w))

    event = _make_touch_event()
    mgr.start_observation(event)

    # Trade within window
    mgr.on_bbo(_bbo(ts_offset_s=0))
    mgr.on_trade(_trade(ts_offset_s=10, price=20100.25))

    # Trade after window → triggers completion
    mgr.on_trade(_trade(ts_offset_s=301, price=20105.00))

    # Another trade → should not accumulate
    mgr.on_trade(_trade(ts_offset_s=400, price=20110.00))

    # Only 1 trade in the completed window (the one within 5 min)
    assert len(completed) == 1
    assert len(completed[0].trades_accumulated) == 1


def test_empty_window_still_computes():
    """Window with zero trades produces features with zero/default values."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    completed: list[ObservationWindow] = []
    mgr.on_observation_complete(lambda w: completed.append(w))

    event = _make_touch_event()
    mgr.start_observation(event)

    # No trades during window — just trigger completion
    mgr.on_trade(_trade(ts_offset_s=301))

    assert len(completed) == 1
    assert completed[0].features is not None
    assert completed[0].features["int_time_beyond_level"] == 0.0
    assert completed[0].features["int_absorption_ratio"] == 0.0


def test_observation_stored_has_features():
    """Completed observation has all 3 feature keys."""
    fc = FeatureComputer()
    mgr = ObservationManager(fc)

    completed: list[ObservationWindow] = []
    mgr.on_observation_complete(lambda w: completed.append(w))

    event = _make_touch_event()
    mgr.start_observation(event)

    mgr.on_bbo(_bbo(ts_offset_s=0))
    mgr.on_trade(_trade(ts_offset_s=10, price=20100.00, size=10))
    mgr.on_trade(_trade(ts_offset_s=301))

    assert len(completed) == 1
    features = completed[0].features
    assert isinstance(features, dict)
    assert set(features.keys()) == {
        "int_time_beyond_level",
        "int_time_within_2pts",
        "int_absorption_ratio",
    }
