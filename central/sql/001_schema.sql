-- Central server schema for the urban intelligence platform prototype.
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS postgis;

-- ======================================================================================
-- raw_events - everything the fleet reports, confirmed or not.
-- ======================================================================================
-- Note: the API field is `timestamp`; the column is `event_time` to avoid colliding
-- with the SQL type name.
CREATE TABLE IF NOT EXISTS raw_events (
    event_id        UUID PRIMARY KEY,               -- generated on the bus (idempotency key)
    schema_version  TEXT        NOT NULL,
    bus_id          TEXT        NOT NULL,
    camera_id       TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,

    event_time      TIMESTAMPTZ NOT NULL,           -- bus clock, when it happened
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- server clock, when it arrived
                                                    -- (differ by hours after a connectivity gap)

    bus_geog        GEOGRAPHY(POINT, 4326) NOT NULL,     -- where the BUS was
    defect_geog     GEOGRAPHY(POINT, 4326),              -- bus position projected forward along
                                                         -- heading by est_range_m; the object's
                                                         -- estimated true position
    gps_accuracy_m  DOUBLE PRECISION,
    heading_deg     DOUBLE PRECISION,
    speed_kmph      DOUBLE PRECISION,

    confidence      DOUBLE PRECISION NOT NULL,
    model_versions  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    bbox            JSONB,
    media_ref       TEXT,
    priority        TEXT        NOT NULL DEFAULT 'batch',
    extra           JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- false when this report was suppressed by the same-bus cooldown. The row is still
    -- kept for audit, but it must not inflate the distinct-bus confirmation count.
    counts_toward_confirmation BOOLEAN NOT NULL DEFAULT TRUE
);

-- the position actually used for clustering: projected defect position where we have
-- one, else the bus position
CREATE OR REPLACE FUNCTION event_geog(raw_events) RETURNS GEOGRAPHY(POINT, 4326)
    LANGUAGE SQL IMMUTABLE AS $$ SELECT COALESCE($1.defect_geog, $1.bus_geog) $$;

CREATE INDEX IF NOT EXISTS raw_events_bus_geog_idx    ON raw_events USING GIST (bus_geog);
CREATE INDEX IF NOT EXISTS raw_events_defect_geog_idx ON raw_events USING GIST (defect_geog);
CREATE INDEX IF NOT EXISTS raw_events_type_time_idx   ON raw_events (event_type, event_time DESC);
CREATE INDEX IF NOT EXISTS raw_events_bus_time_idx    ON raw_events (bus_id, event_time DESC);
CREATE INDEX IF NOT EXISTS raw_events_extra_idx       ON raw_events USING GIN (extra);

-- ======================================================================================
-- confirmed_potholes - persistent defects, only after multiple distinct buses agree.
-- ======================================================================================
CREATE TABLE IF NOT EXISTS confirmed_potholes (
    id              BIGSERIAL PRIMARY KEY,
    geog            GEOGRAPHY(POINT, 4326) NOT NULL,   -- cluster centroid
    report_count    INTEGER     NOT NULL,
    distinct_buses  INTEGER     NOT NULL,
    avg_confidence  DOUBLE PRECISION,
    severity        TEXT,                              -- worst severity in the cluster
    first_seen_at   TIMESTAMPTZ NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL,
    -- 'confirmed' | 'resolved'. Potholes get repaired: without demotion the map only
    -- ever accumulates.
    status          TEXT        NOT NULL DEFAULT 'confirmed',
    resolved_at     TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS confirmed_potholes_geog_idx   ON confirmed_potholes USING GIST (geog);
CREATE INDEX IF NOT EXISTS confirmed_potholes_status_idx ON confirmed_potholes (status);

-- ======================================================================================
-- confirmed_waterlogging - transient. Short window, and it goes stale on its own.
-- ======================================================================================
CREATE TABLE IF NOT EXISTS confirmed_waterlogging (
    id              BIGSERIAL PRIMARY KEY,
    geog            GEOGRAPHY(POINT, 4326) NOT NULL,
    report_count    INTEGER     NOT NULL,
    distinct_buses  INTEGER     NOT NULL,
    avg_confidence  DOUBLE PRECISION,
    avg_coverage_pct DOUBLE PRECISION,
    first_seen_at   TIMESTAMPTZ NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL,
    -- 'confirmed' | 'expired'
    status          TEXT        NOT NULL DEFAULT 'confirmed',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS confirmed_waterlogging_geog_idx   ON confirmed_waterlogging USING GIST (geog);
CREATE INDEX IF NOT EXISTS confirmed_waterlogging_status_idx ON confirmed_waterlogging (status);

-- ======================================================================================
-- Congestion is NOT confirmed and NOT materialised - it is aggregated at query time
-- straight out of raw_events (grid cell x time bucket). See layers.py.
-- Incidents are likewise served directly from raw_events: a single high-confidence
-- report is enough, so there is nothing to confirm or aggregate.
-- ======================================================================================
