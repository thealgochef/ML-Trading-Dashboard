"""Initial schema — config, connection_events, ohlcv_bars, model_versions.

Revision ID: 001
Revises: None
Create Date: 2026-03-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "connection_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "ohlcv_bars",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeframe", sa.String(), nullable=False),
        sa.Column("open", sa.Numeric(12, 2), nullable=False),
        sa.Column("high", sa.Numeric(12, 2), nullable=False),
        sa.Column("low", sa.Numeric(12, 2), nullable=False),
        sa.Column("close", sa.Numeric(12, 2), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.UniqueConstraint("timestamp", "timeframe", "symbol", name="uq_bar_ts_tf_sym"),
    )

    op.create_table(
        "model_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("model_versions")
    op.drop_table("ohlcv_bars")
    op.drop_table("connection_events")
    op.drop_table("config")
