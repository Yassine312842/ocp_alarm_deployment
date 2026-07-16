-- Production storage schema (TimescaleDB / PostgreSQL).
-- Run this once TimescaleDB is available; the DuckDB store mirrors it for local dev.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector, for "similar incident" search

CREATE TABLE IF NOT EXISTS process_samples (
    ts     TIMESTAMPTZ NOT NULL,
    tag    TEXT        NOT NULL,
    value  DOUBLE PRECISION
);
SELECT create_hypertable('process_samples', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_samples_tag_ts ON process_samples (tag, ts DESC);

CREATE TABLE IF NOT EXISTS alarms (
    ts          TIMESTAMPTZ NOT NULL,
    tag         TEXT        NOT NULL,
    level       TEXT,        -- LO / LOLO / HI / HIHI
    priority    TEXT,        -- LOW / HIGH / CRITICAL
    state       TEXT,        -- ACTIVE / CLEARED / ACK
    value       DOUBLE PRECISION,
    incident_id TEXT
);
SELECT create_hypertable('alarms', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_alarms_tag_ts ON alarms (tag, ts DESC);

-- Knowledge base of confirmed incidents (Layer 4). Operators validate these,
-- which is what makes the system smarter over time.
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    start_ts    TIMESTAMPTZ,
    end_ts      TIMESTAMPTZ,
    scenario    TEXT,
    root_cause  TEXT,
    confirmed   BOOLEAN DEFAULT FALSE,
    signature   VECTOR(64)   -- embedding of the alarm sequence for similarity search
);
