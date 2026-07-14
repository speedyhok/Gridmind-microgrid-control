-- Enable TimescaleDB extension if available
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- 1. Static Metadata Table: assets
CREATE TABLE IF NOT EXISTS assets (
    asset_id VARCHAR(50) PRIMARY KEY,
    asset_type VARCHAR(20) NOT NULL, -- 'solar', 'wind', 'battery', 'building'
    capacity_kw DOUBLE PRECISION,
    location VARCHAR(100)
);

-- 2. Time-Series Table: electricity_prices
CREATE TABLE IF NOT EXISTS electricity_prices (
    time TIMESTAMPTZ NOT NULL,
    buy_price_inr DOUBLE PRECISION NOT NULL,
    sell_price_inr DOUBLE PRECISION NOT NULL,
    grid_frequency DOUBLE PRECISION NOT NULL
);

-- Convert to hypertable for TimescaleDB (if using Timescale)
SELECT create_hypertable('electricity_prices', 'time', if_not_exists => TRUE);

-- 3. Time-Series Table: telemetry_power
CREATE TABLE IF NOT EXISTS telemetry_power (
    time TIMESTAMPTZ NOT NULL,
    asset_id VARCHAR(50) REFERENCES assets(asset_id),
    power_kw DOUBLE PRECISION NOT NULL,
    voltage DOUBLE PRECISION,
    status VARCHAR(20) NOT NULL, -- 'operational', 'off', 'error'
    anomaly_flag BOOLEAN DEFAULT FALSE
);

-- Convert to hypertable for TimescaleDB (if using Timescale)
SELECT create_hypertable('telemetry_power', 'time', if_not_exists => TRUE);

-- Indexes for performance if hypertables are run in raw PostgreSQL
CREATE INDEX IF NOT EXISTS idx_telemetry_power_asset_time ON telemetry_power (asset_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_electricity_prices_time ON electricity_prices (time DESC);

-- 4. Time-Series Table: predictions
CREATE TABLE IF NOT EXISTS predictions (
    time TIMESTAMPTZ NOT NULL,
    target_type VARCHAR(20) NOT NULL, -- 'demand', 'solar', 'wind', 'price'
    predicted_value DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- Convert to hypertable for TimescaleDB (if using Timescale)
SELECT create_hypertable('predictions', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_predictions_target_time ON predictions (target_type, time DESC);

