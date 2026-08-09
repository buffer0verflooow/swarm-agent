-- ============================================================================
-- Migration 014: Action-Value Scheduling (opt-in)
--
-- Adds the data the value scheduler needs without changing default behaviour.
-- Runs keep the static `priority` ordering unless their config sets
-- `scheduler_policy = "value"`.
--
--   * agent_tasks.base_priority   original hand-assigned priority, captured once
--                                 the first time a run is re-ranked by value, so
--                                 the designer prior is preserved as a feature.
--   * scheduler_decisions         every candidate's value score + features per
--                                 generation, for offline value-vs-priority A/B.
-- ============================================================================

ALTER TABLE agent_tasks ADD COLUMN base_priority INTEGER;

CREATE TABLE IF NOT EXISTS scheduler_decisions (
    decision_id         TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    generation          INTEGER DEFAULT 1,
    signal_fingerprint  TEXT NOT NULL,
    policy              TEXT NOT NULL DEFAULT 'market-action-value-v1',
    value_score         REAL NOT NULL,
    base_priority       INTEGER DEFAULT 50,
    effective_priority  INTEGER DEFAULT 50,
    value_rank          INTEGER DEFAULT 0,
    mode                TEXT NOT NULL DEFAULT 'exploit'
                        CHECK (mode IN ('exploit', 'explore')),
    features            TEXT DEFAULT '{}',
    attempts            INTEGER DEFAULT 0,
    informative         REAL DEFAULT 0,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_decisions_unique
    ON scheduler_decisions(run_id, task_id, generation);

CREATE INDEX IF NOT EXISTS idx_sched_decisions_run
    ON scheduler_decisions(run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sched_decisions_fp
    ON scheduler_decisions(signal_fingerprint, created_at DESC);
