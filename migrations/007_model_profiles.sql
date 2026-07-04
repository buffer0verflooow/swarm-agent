-- ============================================================================
-- Migration 007: Swarm-owned Model Profiles + Conversation Events
--
-- The swarm owns model/tool policy. External clients such as Claude, Hermes or
-- Codex call into the swarm and receive the model profile selected for a task.
-- ============================================================================

CREATE TABLE IF NOT EXISTS model_profiles (
    profile_id      TEXT PRIMARY KEY,
    role            TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    priority        INTEGER DEFAULT 50,
    is_default      INTEGER DEFAULT 0,
    enabled         INTEGER DEFAULT 1,
    max_tokens      INTEGER,
    temperature     REAL,
    tool_policy     TEXT DEFAULT '{}',
    system_prompt   TEXT,
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(role, provider, model)
);

CREATE INDEX IF NOT EXISTS idx_model_profiles_role
    ON model_profiles(role, enabled, is_default DESC, priority DESC);

ALTER TABLE agent_tasks ADD COLUMN model_profile_id TEXT;
ALTER TABLE agent_profiles ADD COLUMN model_profile_id TEXT;

CREATE INDEX IF NOT EXISTS idx_agent_tasks_model_profile
    ON agent_tasks(model_profile_id);

CREATE TABLE IF NOT EXISTS swarm_conversation_events (
    event_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES swarm_runs(run_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,
    agent_id        TEXT,
    task_id         TEXT,
    content         TEXT NOT NULL,
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sce_run
    ON swarm_conversation_events(run_id, created_at);

ALTER TABLE swarm_runs ADD COLUMN conversation_summary TEXT DEFAULT '';
ALTER TABLE swarm_runs ADD COLUMN summary_updated_at TEXT;

-- Default profiles are intentionally provider=client: the swarm owns the
-- policy class, while the invoking client maps it to a concrete Claude/Codex/etc
-- model if no project-specific override has been configured yet.
INSERT OR IGNORE INTO model_profiles
    (profile_id, role, provider, model, priority, is_default, max_tokens, temperature, tool_policy, system_prompt)
VALUES
    ('default-scanner-fast', 'scanner', 'client', 'fast', 80, 1, 12000, 0.2,
     '{"network": true, "shell": true, "write": false}',
     'You are a scanner worker. Explore breadth first, capture concrete findings, and avoid assuming impact without evidence.'),
    ('default-analyst-reasoning', 'analyst', 'client', 'reasoning', 80, 1, 20000, 0.2,
     '{"network": false, "shell": true, "write": false}',
     'You are an analyst worker. Form hypotheses, verify evidence, and produce concise technical conclusions.'),
    ('default-exploiter-careful', 'exploiter', 'client', 'careful', 80, 1, 20000, 0.1,
     '{"network": true, "shell": true, "write": false, "destructive": false}',
     'You are an exploiter worker operating only within explicit authorization. Validate impact carefully and stop before destructive action.'),
    ('default-reporter-writer', 'reporter', 'client', 'writer', 80, 1, 16000, 0.3,
     '{"network": false, "shell": false, "write": true}',
     'You are a reporter worker. Summarize verified evidence, uncertainty, impact, and remediation.'),
    ('default-custom-balanced', 'custom', 'client', 'balanced', 50, 1, 16000, 0.2,
     '{}',
     'You are a swarm worker. Follow the task context and capture useful knowledge.');
