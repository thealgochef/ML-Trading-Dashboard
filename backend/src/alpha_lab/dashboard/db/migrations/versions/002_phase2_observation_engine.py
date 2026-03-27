"""Phase 2: active_levels and observation_events tables.

Revision ID: 002
Revises: 001
Create Date: 2026-03-02
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "active_levels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("level_type", sa.String(), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("zone_id", sa.String(), nullable=True),
        sa.Column("is_manual", sa.Boolean(), server_default="false"),
        sa.Column("is_touched", sa.Boolean(), server_default="false"),
        sa.Column("touched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "observation_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level_type", sa.String(), nullable=False),
        sa.Column("level_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("zone_id", sa.String(), nullable=True),
        sa.Column("trade_direction", sa.String(), nullable=False),
        sa.Column("price_at_touch", sa.Numeric(12, 2), nullable=False),
        sa.Column("session", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("int_time_beyond_level", sa.Float(), nullable=True),
        sa.Column("int_time_within_2pts", sa.Float(), nullable=True),
        sa.Column("int_absorption_ratio", sa.Float(), nullable=True),
        sa.Column("prediction", sa.String(), nullable=True),
        sa.Column("prediction_probabilities", sa.JSON(), nullable=True),
        sa.Column("outcome_resolved", sa.Boolean(), server_default="false"),
        sa.Column("outcome_correct", sa.Boolean(), nullable=True),
        sa.Column("mfe_points", sa.Float(), nullable=True),
        sa.Column("mae_points", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )


def downgrade() -> None:
    op.drop_table("observation_events")
    op.drop_table("active_levels")
