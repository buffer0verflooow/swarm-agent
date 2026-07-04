-- ============================================================================
-- Migration 005: Spawn Claim Recovery
--
-- Adds claimed_at so a crashed orchestrator can release stale spawning claims
-- without waiting for the full request TTL.
-- ============================================================================

ALTER TABLE spawn_requests ADD COLUMN claimed_at TEXT;
CREATE INDEX IF NOT EXISTS idx_sr_claimed_at ON spawn_requests(claimed_at);
