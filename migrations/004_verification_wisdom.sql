-- ============================================================================
-- Migration 004: Verification Pipeline + Wisdom Distillation
--
-- Adds validation_queue table for the independent verification pipeline,
-- and wisdom_distillation tracking columns.
-- ============================================================================

-- 1. validation_queue: HIGH/MEDIUM findings waiting for independent verification
CREATE TABLE IF NOT EXISTS validation_queue (
    validation_id        TEXT PRIMARY KEY,
    knowledge_id        TEXT NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
    run_id              TEXT REFERENCES swarm_runs(run_id) ON DELETE SET NULL,
    requested_by        TEXT NOT NULL,               -- which agent requested validation
    assigned_to         TEXT,                         -- which agent will validate (NULL = unassigned)
    status              TEXT DEFAULT 'pending'
                        CHECK (status IN ('pending','assigned','validating','verified','refuted','timeout')),
    priority            INTEGER DEFAULT 50,
    evidence_hash       TEXT,                         -- hash of original evidence for replay
    original_content    TEXT,                         -- snapshot of content at validation request time
    verdict             TEXT,                         -- confirmed / refuted / inconclusive
    verdict_reason      TEXT,
    validated_at        TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vq_status    ON validation_queue(status);
CREATE INDEX IF NOT EXISTS idx_vq_knowledge ON validation_queue(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_vq_priority ON validation_queue(priority DESC);
CREATE INDEX IF NOT EXISTS idx_vq_run       ON validation_queue(run_id);

-- 2. distilled_rules: add wisdom_distillation_source
ALTER TABLE distilled_rules ADD COLUMN distilled_from_knowledge_ids TEXT DEFAULT '[]';

-- 3. ontology_relations: add co_occurrence_count for auto-discovered relations
ALTER TABLE ontology_relations ADD COLUMN co_occurrence_count INTEGER DEFAULT 0;
ALTER TABLE ontology_relations ADD COLUMN last_observed_at TEXT;
