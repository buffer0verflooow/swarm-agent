-- ============================================================================
-- Migration 009: Bounty Knowledge Loop
--
-- Adds an explicit bug-bounty validation layer on top of generic DIKW entries.
-- Vulnerability entries remain knowledge; finding_hypotheses track whether a
-- candidate finding has passed the gates required before report submission.
-- ============================================================================

CREATE TABLE IF NOT EXISTS finding_hypotheses (
    hypothesis_id       TEXT PRIMARY KEY,
    knowledge_id        TEXT NOT NULL UNIQUE REFERENCES knowledge_entries(id) ON DELETE CASCADE,
    run_id              TEXT REFERENCES swarm_runs(run_id) ON DELETE SET NULL,
    target_id           TEXT DEFAULT '',
    program             TEXT DEFAULT '',
    vulnerability_class TEXT DEFAULT '',
    severity            TEXT DEFAULT 'unknown'
                        CHECK (severity IN (
                            'unknown','info','low','medium','high','critical'
                        )),
    scope_status        TEXT DEFAULT 'unknown'
                        CHECK (scope_status IN (
                            'unknown','in_scope','out_of_scope','needs_review'
                        )),
    reachability        TEXT DEFAULT 'unknown'
                        CHECK (reachability IN (
                            'unknown','external','unauthenticated','low_priv',
                            'authenticated','admin','system_only'
                        )),
    validation_status   TEXT DEFAULT 'hypothesis'
                        CHECK (validation_status IN (
                            'hypothesis','validating','validated','refuted',
                            'negative_knowledge'
                        )),
    expected_payout     REAL DEFAULT 0.0,
    estimated_hours     REAL DEFAULT 0.0,
    competition_factor  REAL DEFAULT 1.0,
    roi_score           REAL DEFAULT 0.0,
    rationale           TEXT DEFAULT '',
    created_by          TEXT DEFAULT 'bounty-loop',
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now')),
    validated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_finding_hypotheses_run
    ON finding_hypotheses(run_id, validation_status);

CREATE INDEX IF NOT EXISTS idx_finding_hypotheses_roi
    ON finding_hypotheses(validation_status, roi_score DESC);

CREATE INDEX IF NOT EXISTS idx_finding_hypotheses_target
    ON finding_hypotheses(target_id, program);

CREATE TABLE IF NOT EXISTS finding_validation_gates (
    gate_id         TEXT PRIMARY KEY,
    hypothesis_id   TEXT NOT NULL REFERENCES finding_hypotheses(hypothesis_id) ON DELETE CASCADE,
    gate_name       TEXT NOT NULL
                    CHECK (gate_name IN (
                        'poc_exists','clean_repro','impactful',
                        'low_priv_reachable','in_scope','deduplicated'
                    )),
    status          TEXT DEFAULT 'pending'
                    CHECK (status IN (
                        'pending','pass','fail','blocked','not_applicable'
                    )),
    evidence        TEXT DEFAULT '',
    verified_by     TEXT DEFAULT '',
    verified_at     TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    UNIQUE (hypothesis_id, gate_name)
);

CREATE INDEX IF NOT EXISTS idx_finding_validation_gates_hypothesis
    ON finding_validation_gates(hypothesis_id, status);

CREATE TABLE IF NOT EXISTS negative_knowledge (
    negative_id     TEXT PRIMARY KEY,
    knowledge_id    TEXT REFERENCES knowledge_entries(id) ON DELETE SET NULL,
    hypothesis_id   TEXT REFERENCES finding_hypotheses(hypothesis_id) ON DELETE SET NULL,
    run_id          TEXT REFERENCES swarm_runs(run_id) ON DELETE SET NULL,
    target_id       TEXT DEFAULT '',
    program         TEXT DEFAULT '',
    reason_type     TEXT NOT NULL
                    CHECK (reason_type IN (
                        'out_of_scope','not_reproducible','no_security_impact',
                        'privilege_unreachable','duplicate','hardened_target',
                        'program_not_worthwhile','false_positive','other'
                    )),
    details         TEXT NOT NULL,
    created_by      TEXT DEFAULT 'bounty-loop',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_negative_knowledge_target
    ON negative_knowledge(target_id, program, reason_type);

CREATE INDEX IF NOT EXISTS idx_negative_knowledge_run
    ON negative_knowledge(run_id, reason_type);
