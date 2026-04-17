"""
Dashboard-specific configuration using Pydantic Settings.

Loads from environment variables with DASHBOARD_ prefix and .env file.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DashboardSettings(BaseSettings):
    """Configuration for the live trading dashboard data pipeline."""

    # Data source: "databento" or "rithmic"
    data_source: str = "databento"

    # Rithmic connection
    rithmic_username: str = ""
    rithmic_password: SecretStr = SecretStr("")
    rithmic_system: str = "APEX"
    rithmic_gateway: str = "Chicago Area"
    rithmic_url: str = ""
    rithmic_app_name: str = "AlphaLab"
    rithmic_app_version: str = "1.0"

    # Databento
    databento_api_key: SecretStr | None = None

    # PostgreSQL
    database_url: str = (
        "postgresql+asyncpg://postgres:alphalab2026@localhost:5432/alpha_lab"
    )

    # Tick recording
    tick_recording_dir: Path = Path("data/rithmic/NQ")

    # Instrument
    symbol: str = "NQ"
    exchange: str = "CME"

    # Price buffer
    price_buffer_hours: int = 48

    # Model management
    model_dir: Path = Path("data/models")

    # Replay data directory (Parquet files organized by date)
    replay_data_dir: Path = Path("../data/databento/NQ")

    # Optional touch-layer level disabling, comma-separated level names.
    # Example: "pdh,pdl"
    disabled_levels: str | None = None

    # Model inference
    min_confidence: float = 0.70  # Min P(tradeable_reversal) to execute

    # Outcome tracking thresholds (match training labels)
    mfe_target: float = 15.0  # TP threshold for tradeable_reversal
    mae_stop: float = 30.0  # SL threshold for trap/blowthrough
    trap_mfe_min: float = 5.0  # Min MFE to distinguish trap from blowthrough

    # Approach feature window
    approach_window_minutes: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DASHBOARD_",
        extra="ignore",
    )
