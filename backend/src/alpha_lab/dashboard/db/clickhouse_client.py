"""
ClickHouse client for tick data storage and retrieval.

Wraps clickhouse_connect to provide typed methods for:
- Inserting live tick recordings (trade + BBO)
- Querying historical MBP-10 data for the replay client
- Discovering available dates and front-month contracts

All methods are optional-use: if ClickHouse is unavailable, the caller
falls back to local Parquet files.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import clickhouse_connect
from clickhouse_connect.driver import Client

if TYPE_CHECKING:
    import pandas as pd

    from alpha_lab.dashboard.config.settings import DashboardSettings

logger = logging.getLogger(__name__)


class ClickHouseTickClient:
    """Thin wrapper around clickhouse_connect for tick data operations."""

    def __init__(self, settings: DashboardSettings) -> None:
        self._settings = settings
        self._client: Client | None = None

    def connect(self) -> None:
        """Establish connection to ClickHouse."""
        self._client = clickhouse_connect.get_client(
            host=self._settings.clickhouse_host,
            port=self._settings.clickhouse_port,
            database=self._settings.clickhouse_database,
            username=self._settings.clickhouse_user,
            password=self._settings.clickhouse_password.get_secret_value(),
        )
        logger.info(
            "Connected to ClickHouse at %s:%d/%s",
            self._settings.clickhouse_host,
            self._settings.clickhouse_port,
            self._settings.clickhouse_database,
        )

    @property
    def client(self) -> Client:
        if self._client is None:
            self.connect()
        return self._client

    # ── Live tick insertion ────────────────────────────────────────

    def insert_live_ticks(self, rows: list[dict]) -> None:
        """Bulk insert live tick recordings into the live_ticks table."""
        if not rows:
            return
        columns = [
            "timestamp", "record_type", "price", "bid_price", "ask_price",
            "bid_size", "ask_size", "trade_size", "aggressor_side", "symbol",
        ]
        data = [[row.get(c) for c in columns] for row in rows]
        self.client.insert("live_ticks", data, column_names=columns)

    # ── MBP-10 queries (for replay client) ─────────────────────────

    def query_mbp10_dates(self) -> list[str]:
        """Get sorted list of available dates with MBP-10 data."""
        result = self.client.query(
            "SELECT DISTINCT toDate(ts_event) AS d FROM mbp10 ORDER BY d"
        )
        return [str(row[0]) for row in result.result_rows]

    def query_mbp10_trades(self, date_str: str, symbol: str) -> pd.DataFrame:
        """Query MBP-10 trade records for a given date and symbol.

        Returns a DataFrame with columns matching replay_client expectations:
        ts_event, price, size, side, bid_price, ask_price, bid_size, ask_size
        """
        return self.client.query_df(
            """
            SELECT
                ts_event,
                price,
                size,
                side,
                bid_px_00 AS bid_price,
                ask_px_00 AS ask_price,
                bid_sz_00 AS bid_size,
                ask_sz_00 AS ask_size
            FROM mbp10
            WHERE toDate(ts_event) = {date:String}
              AND action = 'T'
              AND symbol = {symbol:String}
              AND bid_px_00 > 0 AND ask_px_00 > 0
            ORDER BY ts_event
            """,
            parameters={"date": date_str, "symbol": symbol},
        )

    def detect_front_month(self, date_str: str) -> str:
        """Detect the front-month symbol for a given date (highest trade count)."""
        result = self.client.query(
            """
            SELECT symbol, count(*) AS n
            FROM mbp10
            WHERE toDate(ts_event) = {date:String}
              AND action = 'T'
              AND symbol NOT LIKE '%-%'
            GROUP BY symbol
            ORDER BY n DESC
            LIMIT 1
            """,
            parameters={"date": date_str},
        )
        if not result.result_rows:
            raise ValueError(f"No trades found for {date_str}")
        return result.result_rows[0][0]
