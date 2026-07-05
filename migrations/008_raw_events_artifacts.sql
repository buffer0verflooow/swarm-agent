-- ============================================================================
-- Migration 008: Lossless Raw Events + Verified Artifacts
--
-- raw_agent_events preserves every agent emission before signal filtering so a
-- low-signal knowledge filter cannot break stigmergic handoff.
-- agent_artifacts records parent-side verification of files that agents claim
-- to have produced.
-- ============================================================================

CREATE TABLE IF NOT EXISTS raw_agent_events (
    event_id            TEXT PRIMARY KEY,
    run_id              TEXT,
    task_id             TEXT,
    source_agent        TEXT NOT NULL,
    source              TEXT NOT NULL,
    content             TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    metadata            TEXT DEFAULT '{}',
    signal_count        INTEGER DEFAULT 0,
    capture_status      TEXT DEFAULT 'received'
                        CHECK (capture_status IN (
                            'received','captured','filtered','duplicate','error'
                        )),
    filter_reason       TEXT DEFAULT '',
    knowledge_entry_id  TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_raw_agent_events_run
    ON raw_agent_events(run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_agent_events_task
    ON raw_agent_events(task_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_agent_events_status
    ON raw_agent_events(capture_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_agent_events_hash
    ON raw_agent_events(content_hash);

CREATE TABLE IF NOT EXISTS agent_artifacts (
    artifact_id     TEXT PRIMARY KEY,
    run_id          TEXT,
    task_id         TEXT,
    agent_id        TEXT,
    declared_path   TEXT NOT NULL,
    resolved_path   TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN (
                        'verified','missing','empty','not_file','outside_root','unreadable'
                    )),
    size_bytes      INTEGER DEFAULT 0,
    sha256          TEXT DEFAULT '',
    required        INTEGER DEFAULT 1,
    error           TEXT DEFAULT '',
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_artifacts_task
    ON agent_artifacts(task_id, status);

CREATE INDEX IF NOT EXISTS idx_agent_artifacts_run
    ON agent_artifacts(run_id, status);
