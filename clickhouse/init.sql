-- ClickHouse schema for Trade Dashboard tick data
-- Applied automatically on first container start via docker-entrypoint-initdb.d

CREATE DATABASE IF NOT EXISTS trade_data;

-- ═══════════════════════════════════════════════════════════════════
-- Historical MBP-10 depth-of-book data (imported from Databento Parquet)
-- ═══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trade_data.mbp10 (
    -- Event-level fields
    ts_event        DateTime64(9, 'UTC'),
    rtype           UInt8,
    publisher_id    UInt16,
    instrument_id   UInt32,
    action          LowCardinality(String),
    side            LowCardinality(String),
    depth           UInt8,
    price           Float64,
    size            UInt32,
    flags           UInt8,
    ts_in_delta     Int32,
    sequence        UInt32,
    symbol          LowCardinality(String),
    ts_recv         DateTime64(9, 'UTC'),

    -- 10-level bid prices
    bid_px_00 Float64, bid_px_01 Float64, bid_px_02 Float64, bid_px_03 Float64, bid_px_04 Float64,
    bid_px_05 Float64, bid_px_06 Float64, bid_px_07 Float64, bid_px_08 Float64, bid_px_09 Float64,
    -- 10-level ask prices
    ask_px_00 Float64, ask_px_01 Float64, ask_px_02 Float64, ask_px_03 Float64, ask_px_04 Float64,
    ask_px_05 Float64, ask_px_06 Float64, ask_px_07 Float64, ask_px_08 Float64, ask_px_09 Float64,
    -- 10-level bid sizes
    bid_sz_00 UInt32, bid_sz_01 UInt32, bid_sz_02 UInt32, bid_sz_03 UInt32, bid_sz_04 UInt32,
    bid_sz_05 UInt32, bid_sz_06 UInt32, bid_sz_07 UInt32, bid_sz_08 UInt32, bid_sz_09 UInt32,
    -- 10-level ask sizes
    ask_sz_00 UInt32, ask_sz_01 UInt32, ask_sz_02 UInt32, ask_sz_03 UInt32, ask_sz_04 UInt32,
    ask_sz_05 UInt32, ask_sz_06 UInt32, ask_sz_07 UInt32, ask_sz_08 UInt32, ask_sz_09 UInt32,
    -- 10-level bid order counts
    bid_ct_00 UInt32, bid_ct_01 UInt32, bid_ct_02 UInt32, bid_ct_03 UInt32, bid_ct_04 UInt32,
    bid_ct_05 UInt32, bid_ct_06 UInt32, bid_ct_07 UInt32, bid_ct_08 UInt32, bid_ct_09 UInt32,
    -- 10-level ask order counts
    ask_ct_00 UInt32, ask_ct_01 UInt32, ask_ct_02 UInt32, ask_ct_03 UInt32, ask_ct_04 UInt32,
    ask_ct_05 UInt32, ask_ct_06 UInt32, ask_ct_07 UInt32, ask_ct_08 UInt32, ask_ct_09 UInt32
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(ts_event)
ORDER BY (symbol, ts_event)
SETTINGS index_granularity = 8192;


-- ═══════════════════════════════════════════════════════════════════
-- Live tick recordings (from Databento/Rithmic tick_recorder)
-- ═══════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trade_data.live_ticks (
    timestamp       DateTime64(9, 'UTC'),
    record_type     LowCardinality(String),  -- 'trade' or 'bbo'
    price           Nullable(Float64),
    bid_price       Nullable(Float64),
    ask_price       Nullable(Float64),
    bid_size        Nullable(Int32),
    ask_size        Nullable(Int32),
    trade_size      Nullable(Int32),
    aggressor_side  Nullable(LowCardinality(String)),
    symbol          LowCardinality(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (symbol, timestamp)
SETTINGS index_granularity = 8192;
