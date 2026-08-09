-- AWA Workplace Utilisation & Benchmarking Platform
-- Migration 0001: core multi-tenant schema (spec Section 3).
-- Every table carries client/building lineage so the pooled corpus,
-- anonymisation and benchmarking work from day one.

CREATE TABLE IF NOT EXISTS client (
    client_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    sector      TEXT NOT NULL,
    size_band   TEXT NOT NULL,          -- small / medium / large / enterprise
    region      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- API credentials. Only a SHA-256 hash of the key is stored.
CREATE TABLE IF NOT EXISTS api_key (
    key_hash    TEXT PRIMARY KEY,
    client_id   TEXT REFERENCES client(client_id),  -- NULL for admin keys
    role        TEXT NOT NULL CHECK (role IN ('client', 'admin')),
    label       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS building (
    building_id   TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL REFERENCES client(client_id),
    name          TEXT NOT NULL,
    gross_area_m2 REAL,
    region        TEXT NOT NULL,
    size_band     TEXT NOT NULL,        -- derived from gross_area_m2 at creation
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_building_client ON building(client_id);

CREATE TABLE IF NOT EXISTS study (
    study_id      TEXT PRIMARY KEY,
    building_id   TEXT NOT NULL REFERENCES building(building_id),
    client_id     TEXT NOT NULL REFERENCES client(client_id),
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    interval_mins INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'ingested', 'validated')),
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_building ON study(building_id);
CREATE INDEX IF NOT EXISTS idx_study_client ON study(client_id);

CREATE TABLE IF NOT EXISTS team (
    team_id         TEXT PRIMARY KEY,
    building_id     TEXT NOT NULL REFERENCES building(building_id),
    client_id       TEXT NOT NULL REFERENCES client(client_id),
    name            TEXT NOT NULL,
    allocated_desks INTEGER NOT NULL DEFAULT 0,
    UNIQUE (building_id, name)
);
CREATE INDEX IF NOT EXISTS idx_team_building ON team(building_id);

CREATE TABLE IF NOT EXISTS setting (
    setting_id        TEXT PRIMARY KEY,
    building_id       TEXT NOT NULL REFERENCES building(building_id),
    client_id         TEXT NOT NULL REFERENCES client(client_id),
    code              TEXT NOT NULL,    -- client's label, e.g. "D-3-041"
    type              TEXT NOT NULL,    -- desk / meeting_room / breakout / other
    floor             TEXT,
    zone              TEXT,
    capacity          INTEGER NOT NULL DEFAULT 1,
    allocated_team_id TEXT REFERENCES team(team_id),
    area_m2           REAL,
    UNIQUE (building_id, code)
);
CREATE INDEX IF NOT EXISTS idx_setting_building ON setting(building_id);

CREATE TABLE IF NOT EXISTS person_assignment (
    person_ref      TEXT NOT NULL,      -- client-side pseudonymous reference
    building_id     TEXT NOT NULL REFERENCES building(building_id),
    client_id       TEXT NOT NULL REFERENCES client(client_id),
    team_id         TEXT REFERENCES team(team_id),
    employment_type TEXT NOT NULL CHECK (employment_type IN ('employee', 'contractor')),
    PRIMARY KEY (building_id, person_ref)
);
CREATE INDEX IF NOT EXISTS idx_person_building ON person_assignment(building_id);

CREATE TABLE IF NOT EXISTS round (
    round_id    TEXT PRIMARY KEY,
    study_id    TEXT NOT NULL REFERENCES study(study_id),
    client_id   TEXT NOT NULL REFERENCES client(client_id),
    label       TEXT NOT NULL,          -- client's round label, e.g. "Mon-09:30"
    ts          TEXT NOT NULL,
    day_of_week TEXT NOT NULL,
    UNIQUE (study_id, label)
);
CREATE INDEX IF NOT EXISTS idx_round_study ON round(study_id);

-- The atomic record. status distinguishes a person actually present
-- ('occupied') from signs-of-use with nobody there ('claimed') — the gap
-- between frequency and occupancy is where insight lives (spec 4.1).
CREATE TABLE IF NOT EXISTS observation (
    observation_id   INTEGER PRIMARY KEY,
    setting_id       TEXT NOT NULL REFERENCES setting(setting_id),
    round_id         TEXT NOT NULL REFERENCES round(round_id),
    study_id         TEXT NOT NULL REFERENCES study(study_id),
    client_id        TEXT NOT NULL REFERENCES client(client_id),
    ts               TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('occupied', 'claimed', 'empty')),
    occupied         INTEGER NOT NULL,  -- 1 iff status = 'occupied'
    occupant_count   INTEGER,           -- optional, meeting rooms
    occupant_team_id TEXT REFERENCES team(team_id),
    UNIQUE (setting_id, round_id)
);
CREATE INDEX IF NOT EXISTS idx_obs_study ON observation(study_id);
CREATE INDEX IF NOT EXISTS idx_obs_round ON observation(round_id);

CREATE TABLE IF NOT EXISTS space_schedule (
    building_id TEXT NOT NULL REFERENCES building(building_id),
    client_id   TEXT NOT NULL REFERENCES client(client_id),
    function    TEXT NOT NULL,          -- desks / meeting / breakout / circulation / ...
    area_m2     REAL NOT NULL,
    pct_of_nia  REAL,
    PRIMARY KEY (building_id, function)
);

CREATE TABLE IF NOT EXISTS experience_survey (
    response_id TEXT NOT NULL,
    study_id    TEXT NOT NULL REFERENCES study(study_id),
    client_id   TEXT NOT NULL REFERENCES client(client_id),
    team_id     TEXT REFERENCES team(team_id),
    item        TEXT NOT NULL,          -- e.g. find_place / sit_with_team / focus_support
    score       REAL NOT NULL,          -- 1-5 Likert
    PRIMARY KEY (study_id, response_id, item)
);
CREATE INDEX IF NOT EXISTS idx_survey_study ON experience_survey(study_id);

-- Cleaning/validation flags raised at ingestion (spec 2.1): nothing is
-- trusted downstream until issues are visible.
CREATE TABLE IF NOT EXISTS ingestion_issue (
    issue_id  INTEGER PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES client(client_id),
    study_id  TEXT REFERENCES study(study_id),
    severity  TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    category  TEXT NOT NULL,
    detail    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issue_study ON ingestion_issue(study_id);
