"""
Phase 5 — Account Routes Tests

Tests account CRUD and payout endpoints. These control the simulated
Apex account portfolio — adding accounts, checking status, and
requesting payouts.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_list_accounts(async_client):
    """GET /api/accounts returns all accounts and portfolio summary."""
    resp = await async_client.get("/api/accounts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["accounts"]) == 2
    assert "summary" in data
    assert data["summary"]["total_accounts"] == 2
    assert data["summary"]["active_count"] == 2


@pytest.mark.asyncio
async def test_add_account(async_client):
    """POST /api/accounts creates a new account."""
    resp = await async_client.post("/api/accounts", json={
        "label": "Apex #3",
        "eval_cost": 167.0,
        "activation_cost": 79.0,
        "group": "A",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["account"]["label"] == "Apex #3"
    assert data["account"]["group"] == "A"
    assert data["account"]["balance"] == 50000.0
    assert data["account"]["status"] == "active"

    # Verify it shows up in list
    list_resp = await async_client.get("/api/accounts")
    assert len(list_resp.json()["accounts"]) == 3


@pytest.mark.asyncio
async def test_get_single_account(async_client, app_state):
    """GET /api/accounts/{account_id} returns account detail."""
    acct_id = app_state.account_manager.get_all_accounts()[0].account_id
    resp = await async_client.get(f"/api/accounts/{acct_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["account"]["account_id"] == acct_id
    assert "trade_history" in data


@pytest.mark.asyncio
async def test_request_payout(async_client, app_state):
    """POST /api/accounts/{id}/payout rejects ineligible payout."""
    acct_id = app_state.account_manager.get_all_accounts()[0].account_id
    # Fresh account — not payout eligible
    resp = await async_client.post(
        f"/api/accounts/{acct_id}/payout", json={"amount": 500.0},
    )
    assert resp.status_code == 400
    assert "rejected" in resp.json()["error"].lower()
