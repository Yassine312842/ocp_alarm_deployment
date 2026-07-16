-- OCP Alarm Intelligence — Knowledge Base schema (Postgres)
-- Wires the "Confirm root cause" action to real persistence instead of a stub.
-- Run against the TimescaleDB/Postgres instance already defined in docker-compose.yml.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector, for the "similar past incident" retrieval (Tier 2 item)

-- ---------------------------------------------------------------------------
-- An "incident" is a clustered burst of alarms the RCA engine has grouped
-- together and proposed one or more root-cause candidates for.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at        TIMESTAMPTZ NOT NULL,
    ended_at          TIMESTAMPTZ,
    unit              TEXT,                 -- process unit / area, if you track it
    alarm_count       INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'root_cause_confirmed', 'dismissed')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_incidents_started_at ON incidents (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status);

-- ---------------------------------------------------------------------------
-- The alarm events that make up an incident (many-to-one).
-- If you already have a raw alarms table from the acquisition layer, replace
-- this with a foreign key into it instead of duplicating rows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incident_alarms (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    incident_id       UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    tag               TEXT NOT NULL,
    description       TEXT,
    priority          TEXT,                 -- e.g. 'critical' | 'high' | 'medium' | 'low'
    occurred_at       TIMESTAMPTZ NOT NULL,
    operator_id       TEXT,
    sequence_rank     INTEGER               -- position in the alarm flood (1 = earliest)
);

CREATE INDEX IF NOT EXISTS idx_incident_alarms_incident ON incident_alarms (incident_id);
CREATE INDEX IF NOT EXISTS idx_incident_alarms_occurred_at ON incident_alarms (occurred_at);

-- ---------------------------------------------------------------------------
-- Root-cause candidates proposed by the analysis engine (heuristic today,
-- Granger/Bayesian/vector-retrieval later — this table doesn't need to change
-- when the ranking method improves, only how rows get inserted).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS root_cause_candidates (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    incident_id       UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    tag               TEXT NOT NULL,
    method            TEXT NOT NULL DEFAULT 'earliest_alarm',  -- 'earliest_alarm' | 'granger' | 'bayesian_net' | 'vector_retrieval'
    rank              INTEGER NOT NULL,      -- 1 = top candidate
    confidence        REAL,                  -- 0..1, nullable for methods that don't score
    explanation       TEXT,                  -- human-readable rationale shown in the UI
    embedding         vector(384),           -- for similarity search against past incidents; nullable until Tier-2 lands
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rcc_incident ON root_cause_candidates (incident_id);
-- ANN index for the future vector-search retrieval; harmless if unused today.
CREATE INDEX IF NOT EXISTS idx_rcc_embedding ON root_cause_candidates
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---------------------------------------------------------------------------
-- The operator feedback loop: this is what "Confirm root cause" writes to.
-- One incident can have multiple confirmation events over time (e.g. an
-- operator revises an earlier confirmation), so this is append-only, not
-- an update — that keeps the loop auditable and gives you clean training
-- data later for the AI/vector-retrieval layer.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operator_confirmations (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    incident_id       UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    candidate_id      UUID REFERENCES root_cause_candidates(id),  -- NULL if operator entered a free-text cause not proposed by the engine
    confirmed_tag     TEXT NOT NULL,          -- the tag the operator actually confirmed (may differ from candidate.tag)
    free_text_note    TEXT,
    operator_id       TEXT NOT NULL,
    confirmed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- was the engine's #1-ranked candidate correct? Precomputed on insert for fast accuracy queries.
    engine_top_pick_correct BOOLEAN
);

CREATE INDEX IF NOT EXISTS idx_confirmations_incident ON operator_confirmations (incident_id);
CREATE INDEX IF NOT EXISTS idx_confirmations_operator ON operator_confirmations (operator_id);

-- Trigger to keep incidents.status and updated_at in sync when a confirmation lands.
CREATE OR REPLACE FUNCTION mark_incident_confirmed() RETURNS TRIGGER AS $$
BEGIN
    UPDATE incidents
    SET status = 'root_cause_confirmed', updated_at = now()
    WHERE id = NEW.incident_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_mark_incident_confirmed ON operator_confirmations;
CREATE TRIGGER trg_mark_incident_confirmed
    AFTER INSERT ON operator_confirmations
    FOR EACH ROW EXECUTE FUNCTION mark_incident_confirmed();

-- ---------------------------------------------------------------------------
-- Convenience view: engine accuracy over time — how often does the #1
-- ranked candidate match what the operator actually confirmed? This is the
-- metric that proves the feedback loop is working and justifies moving to
-- Granger/Bayesian/vector methods.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW engine_accuracy AS
SELECT
    date_trunc('day', oc.confirmed_at) AS day,
    rcc.method,
    count(*) FILTER (WHERE oc.engine_top_pick_correct) AS correct_count,
    count(*) AS total_count,
    round(
        100.0 * count(*) FILTER (WHERE oc.engine_top_pick_correct) / NULLIF(count(*), 0),
        1
    ) AS accuracy_pct
FROM operator_confirmations oc
JOIN root_cause_candidates rcc ON rcc.incident_id = oc.incident_id AND rcc.rank = 1
GROUP BY 1, 2
ORDER BY 1 DESC;
