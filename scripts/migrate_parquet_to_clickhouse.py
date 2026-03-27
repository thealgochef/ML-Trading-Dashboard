"""
Migrate Databento MBP-10 Parquet files into ClickHouse.

Usage:
    python migrate_parquet_to_clickhouse.py /data/migration/NQ

Processes one date directory at a time, inserting in batches of 500K rows.
Tracks progress in a checkpoint file so it can be resumed if interrupted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import clickhouse_connect
import pyarrow.parquet as pq

BATCH_SIZE = 500_000
CHECKPOINT_FILE = Path("migration_checkpoint.json")


def load_checkpoint() -> set[str]:
    if CHECKPOINT_FILE.exists():
        return set(json.loads(CHECKPOINT_FILE.read_text()))
    return set()


def save_checkpoint(done: set[str]) -> None:
    CHECKPOINT_FILE.write_text(json.dumps(sorted(done)))


def migrate_mbp10(data_dir: Path, ch_host: str = "localhost", ch_port: int = 8123) -> None:
    """Migrate all MBP-10 Parquet files from data_dir into ClickHouse."""
    client = clickhouse_connect.get_client(
        host=ch_host,
        port=ch_port,
        database="trade_data",
    )

    # Discover date directories
    dates = sorted(
        d.name for d in data_dir.iterdir()
        if d.is_dir() and (d / "mbp10.parquet").exists()
    )

    done = load_checkpoint()
    remaining = [d for d in dates if d not in done]

    print(f"Total dates: {len(dates)}, already done: {len(done)}, remaining: {len(remaining)}")

    for i, date_str in enumerate(remaining):
        parquet_path = data_dir / date_str / "mbp10.parquet"

        table = pq.read_table(parquet_path)
        total_rows = table.num_rows

        # Insert in batches
        for start in range(0, total_rows, BATCH_SIZE):
            end = min(start + BATCH_SIZE, total_rows)
            batch = table.slice(start, end - start)
            df = batch.to_pandas()

            # ts_recv is the index in Databento Parquet — reset so it's a column
            if df.index.name == "ts_recv":
                df = df.reset_index()

            client.insert_df("mbp10", df)

        print(f"  [{i + 1}/{len(remaining)}] {date_str}: {total_rows:,} rows inserted")
        done.add(date_str)
        save_checkpoint(done)

    print(f"\nMigration complete: {len(done)} dates processed")


def migrate_live_ticks(data_dir: Path, ch_host: str = "localhost", ch_port: int = 8123) -> None:
    """Migrate Rithmic live tick Parquet files into ClickHouse."""
    client = clickhouse_connect.get_client(
        host=ch_host,
        port=ch_port,
        database="trade_data",
    )

    parquet_files = sorted(data_dir.glob("*.parquet"))
    if not parquet_files:
        print(f"No Parquet files found in {data_dir}")
        return

    for f in parquet_files:
        table = pq.read_table(f)
        df = table.to_pandas()
        if df.index.name and df.index.name != "":
            df = df.reset_index()
        client.insert_df("live_ticks", df)
        print(f"  Migrated {f.name}: {len(df):,} rows")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python migrate_parquet_to_clickhouse.py <mbp10_data_dir> [live_ticks_dir]")
        print("  mbp10_data_dir:  Directory containing date subdirs with mbp10.parquet")
        print("  live_ticks_dir:  Optional — directory containing daily live tick Parquet files")
        sys.exit(1)

    mbp10_dir = Path(sys.argv[1])
    print(f"=== Migrating MBP-10 data from {mbp10_dir} ===")
    migrate_mbp10(mbp10_dir)

    if len(sys.argv) >= 3:
        live_dir = Path(sys.argv[2])
        print(f"\n=== Migrating live ticks from {live_dir} ===")
        migrate_live_ticks(live_dir)
