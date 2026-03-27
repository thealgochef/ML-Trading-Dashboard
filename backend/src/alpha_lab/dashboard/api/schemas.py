"""
Phase 5 — Pydantic request/response schemas for the API layer.

Defines typed request bodies for REST endpoints. Response bodies are
plain dicts serialized by FastAPI — no complex nested Pydantic models
needed since the data comes from in-memory dataclasses.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Trading ───────────────────────────────────────────────────────

class CloseAllRequest(BaseModel):
    reason: str = "manual"


class CloseAccountRequest(BaseModel):
    reason: str = "manual"


class ManualEntryRequest(BaseModel):
    direction: str = Field(pattern="^(long|short)$")


# ── Accounts ──────────────────────────────────────────────────────

class AddAccountRequest(BaseModel):
    label: str
    eval_cost: float
    activation_cost: float
    group: str = Field(pattern="^[AB]$")


class PayoutRequest(BaseModel):
    amount: float = Field(gt=0)


# ── Config ────────────────────────────────────────────────────────

class ConfigUpdateRequest(BaseModel):
    group_a_tp: float | None = None
    group_b_tp: float | None = None
    group_a_sl: float | None = None
    group_b_sl: float | None = None
    second_signal_mode: str | None = Field(default=None, pattern="^(ignore|flip)$")


class OverlayUpdateRequest(BaseModel):
    overlays: dict[str, bool]


# ── Levels ────────────────────────────────────────────────────────

class AddManualLevelRequest(BaseModel):
    price: float
