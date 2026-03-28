"""Tests for payout-adjusted DD tracking helpers in run_strategy_comparison.py."""

from __future__ import annotations

from run_strategy_comparison import (
    _init_balance_tracking_record,
    _reset_dd_baseline_after_payout,
    _update_balance_tracking,
)


def test_payout_resets_dd_baseline_not_trough_or_peak() -> None:
    tracking = {"A1": _init_balance_tracking_record(50_000.0)}

    # Build history: up to 52k, down to 49k
    _update_balance_tracking(tracking, "A1", 52_000.0)
    _update_balance_tracking(tracking, "A1", 49_000.0)
    rec_before = dict(tracking["A1"])

    # Payout drops cash balance to 50k (withdrawal, not trading loss)
    _reset_dd_baseline_after_payout(tracking, "A1", 50_000.0)
    rec_after = tracking["A1"]

    assert rec_after["peak"] == rec_before["peak"] == 52_000.0
    assert rec_after["trough"] == rec_before["trough"] == 49_000.0
    assert rec_after["dd_hwm"] == 50_000.0
    # Historical max DD is preserved
    assert rec_after["dd_max"] == rec_before["dd_max"] == 3_000.0


def test_post_payout_loss_uses_new_dd_baseline() -> None:
    tracking = {"A1": _init_balance_tracking_record(50_000.0)}
    _update_balance_tracking(tracking, "A1", 52_000.0)

    # Reset DD baseline after payout withdrawal to 50,500
    _reset_dd_baseline_after_payout(tracking, "A1", 50_500.0)

    # Trading loss after payout: 50,500 -> 49,900 = 600 DD from new baseline
    _update_balance_tracking(tracking, "A1", 49_900.0)

    assert tracking["A1"]["dd_hwm"] == 50_500.0
    assert tracking["A1"]["dd_max"] == 600.0


def test_payout_withdrawal_alone_does_not_inflate_dd() -> None:
    tracking = {"A1": _init_balance_tracking_record(50_000.0)}
    _update_balance_tracking(tracking, "A1", 52_000.0)
    # DD before payout: 52k -> 50k = 2k
    _update_balance_tracking(tracking, "A1", 50_000.0)
    assert tracking["A1"]["dd_max"] == 2_000.0

    # Payout lowers cash balance to 49k; reset baseline to exclude withdrawal
    _reset_dd_baseline_after_payout(tracking, "A1", 49_000.0)
    assert tracking["A1"]["dd_hwm"] == 49_000.0
    assert tracking["A1"]["dd_max"] == 2_000.0


def test_trough_remains_true_full_run_low_after_payout_reset() -> None:
    tracking = {"A1": _init_balance_tracking_record(50_000.0)}
    _update_balance_tracking(tracking, "A1", 49_500.0)
    _update_balance_tracking(tracking, "A1", 49_200.0)
    _reset_dd_baseline_after_payout(tracking, "A1", 50_100.0)

    # Trough remains whole-run low
    assert tracking["A1"]["trough"] == 49_200.0
