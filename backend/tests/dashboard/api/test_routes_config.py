"""
Phase 5 — Config Routes Tests

Tests configuration endpoints for TP/SL settings, signal mode,
and chart overlay toggles.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_config(async_client):
    """GET /api/config returns current TP/SL and signal mode."""
    resp = await async_client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["group_a_tp"] == 15.0
    assert data["group_b_tp"] == 30.0
    assert data["group_a_sl"] == 15.0
    assert data["group_b_sl"] == 30.0
    assert data["second_signal_mode"] == "ignore"


@pytest.mark.asyncio
async def test_update_config(async_client):
    """PUT /api/config changes TP and verifies via GET."""
    resp = await async_client.put("/api/config", json={
        "group_a_tp": 20.0,
        "second_signal_mode": "flip",
    })
    assert resp.status_code == 200
    config = resp.json()["config"]
    assert config["group_a_tp"] == 20.0
    assert config["second_signal_mode"] == "flip"
    # Other values unchanged
    assert config["group_b_tp"] == 30.0

    # Verify persistence via GET
    get_resp = await async_client.get("/api/config")
    assert get_resp.json()["group_a_tp"] == 20.0
    assert get_resp.json()["second_signal_mode"] == "flip"


@pytest.mark.asyncio
async def test_get_overlays(async_client):
    """GET /api/config/overlays returns overlay toggles."""
    resp = await async_client.get("/api/config/overlays")
    assert resp.status_code == 200
    overlays = resp.json()["overlays"]
    assert overlays["ema_13"] is True
    assert overlays["vwap"] is False


@pytest.mark.asyncio
async def test_update_overlays(async_client):
    """PUT /api/config/overlays updates toggles."""
    resp = await async_client.put("/api/config/overlays", json={
        "overlays": {"vwap": True, "ema_13": False},
    })
    assert resp.status_code == 200
    overlays = resp.json()["overlays"]
    assert overlays["vwap"] is True
    assert overlays["ema_13"] is False
    # Unchanged
    assert overlays["ema_48"] is True
