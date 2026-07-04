-- ============================================================================
-- Migration 006: Swarm Work Market
--
-- Turns agent_tasks into a shared task market. Agents claim work by role from
-- the same pending pool instead of receiving only linear stage handoffs.
-- ============================================================================

ALTER TABLE agent_tasks ADD COLUMN required_role TEXT;
ALTER TABLE agent_tasks ADD COLUMN priority INTEGER DEFAULT 50;
ALTER TABLE agent_tasks ADD COLUMN claimed_at TEXT;
ALTER TABLE agent_tasks ADD COLUMN signal_key TEXT;
ALTER TABLE agent_tasks ADD COLUMN claim_count INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_agent_tasks_market
    ON agent_tasks(run_id, status, required_role, priority DESC, created_at);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_claimed_at
    ON agent_tasks(claimed_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_signal_active
    ON agent_tasks(run_id, signal_key)
    WHERE signal_key IS NOT NULL AND status IN ('pending', 'running');
