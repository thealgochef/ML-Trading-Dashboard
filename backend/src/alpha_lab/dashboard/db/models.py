"""
SQLAlchemy 2.0 models for the dashboard operational database.

Tables:
- config: Key/value store for settings and state
- connection_events: Rithmic connection status log
- ohlcv_bars: Recent OHLCV bars for chart rendering
- model_versions: CatBoost model file tracking
- active_levels: Phase 2 — daily key levels for observation engine
- observation_events: Phase 2 — historical observation records
- apex_accounts: Phase 4 — simulated Apex trading accounts
- trades: Phase 4 — trade history
- daily_account_snapshots: Phase 4 — daily equity snapshots
- payouts: Phase 4 — payout history
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class Config(Base):
    """Key/value configuration store with JSON values."""

    __tablename__ = "config"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ConnectionEvent(Base):
    """Log of Rithmic connection status changes."""

    __tablename__ = "connection_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class OHLCVBar(Base):
    """Recent OHLCV bars for chart rendering."""

    __tablename__ = "ohlcv_bars"
    __table_args__ = (
        UniqueConstraint("timestamp", "timeframe", "symbol", name="uq_bar_ts_tf_sym"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timeframe: Mapped[str] = mapped_column(String, nullable=False)
    open: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)


class ModelVersion(Base):
    """CatBoost model version tracking."""

    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ActiveLevel(Base):
    """Active key levels for the current trading day."""

    __tablename__ = "active_levels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    level_type: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    is_touched: Mapped[bool] = mapped_column(Boolean, default=False)
    touched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    available_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)


class ObservationEvent(Base):
    """Historical record of every touch + observation outcome."""

    __tablename__ = "observation_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    level_type: Mapped[str] = mapped_column(String, nullable=False)
    level_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String, nullable=True)
    trade_direction: Mapped[str] = mapped_column(String, nullable=False)
    price_at_touch: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    session: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # Features (null if discarded)
    int_time_beyond_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    int_time_within_2pts: Mapped[float | None] = mapped_column(Float, nullable=True)
    int_absorption_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Prediction (populated by Phase 3)
    prediction: Mapped[str | None] = mapped_column(String, nullable=True)
    prediction_probabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Outcome tracking (populated by Phase 3)
    outcome_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mfe_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    mae_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)


# ── Phase 4: Paper Trading ──────────────────────────────────────


class ApexAccountRecord(Base):
    """Simulated Apex 4.0 account state."""

    __tablename__ = "apex_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    group_name: Mapped[str] = mapped_column(String, nullable=False)
    eval_cost: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    activation_cost: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    balance: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=50000.00,
    )
    peak_balance: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=50000.00,
    )
    liquidation_threshold: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=48000.00,
    )
    safety_net_reached: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payout_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qualifying_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_payouts: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False, default=0.00,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )
    blown_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class TradeRecord(Base):
    """Trade history for simulated accounts."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    direction: Mapped[str] = mapped_column(String, nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    contracts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    pnl: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    pnl_points: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    group_name: Mapped[str] = mapped_column(String, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    mfe_points: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    mae_points: Mapped[float] = mapped_column(Numeric(8, 2), default=0)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)


class DailySnapshot(Base):
    """Daily account snapshots for equity curves."""

    __tablename__ = "daily_account_snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "trading_date", name="uq_account_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    opening_balance: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    closing_balance: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True,
    )
    daily_pnl: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    trades_count: Mapped[int] = mapped_column(Integer, default=0)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    liquidation_threshold: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False,
    )


class PayoutRecord(Base):
    """Payout history for simulated accounts."""

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, nullable=False)
    payout_number: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    balance_before: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    balance_after: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )
