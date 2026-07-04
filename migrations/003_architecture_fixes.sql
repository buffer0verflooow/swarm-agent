-- ============================================================================
-- Migration 003: Architecture Fixes
-- 
-- Adds missing columns for pheromone decay, token tracking, strategy distillation,
-- load-based scheduling, and power schedule.
-- ============================================================================

-- 1. knowledge_entries: add content_hash for real dedup + last_validated_at for decay
ALTER TABLE knowledge_entries ADD COLUMN content_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_ke_content_hash ON knowledge_entries(content_hash);

-- 2. knowledge_entries: pheromone score (decays over time without validation)
ALTER TABLE knowledge_entries ADD COLUMN pheromone REAL DEFAULT 1.0;
ALTER TABLE knowledge_entries ADD COLUMN last_validated_at TEXT;
ALTER TABLE knowledge_entries ADD COLUMN validation_count INTEGER DEFAULT 0;

-- 3. agent_tasks: token tracking (already has token_cost, add estimated_tokens)
ALTER TABLE agent_tasks ADD COLUMN estimated_tokens INTEGER DEFAULT 0;

-- 4. swarm_runs: budget tracking for power schedule
ALTER TABLE swarm_runs ADD COLUMN token_budget INTEGER DEFAULT 100000;
ALTER TABLE swarm_runs ADD COLUMN tokens_spent INTEGER DEFAULT 0;
ALTER TABLE swarm_runs ADD COLUMN budget_strategy TEXT DEFAULT 'balanced'
    CHECK (budget_strategy IN ('breadth','depth','balanced','exploit'));

-- 5. spawn_requests: add chain_depth to track exploit chain depth
ALTER TABLE spawn_requests ADD COLUMN chain_depth INTEGER DEFAULT 0;
ALTER TABLE spawn_requests ADD COLUMN max_chain_depth INTEGER DEFAULT 3;

-- 6. agent_heartbeats: add work_steal_eligible flag
ALTER TABLE agent_heartbeats ADD COLUMN stealable INTEGER DEFAULT 1;

-- 7. distilled_rules: add auto-distillation tracking
ALTER TABLE distilled_rules ADD COLUMN auto_distilled INTEGER DEFAULT 0;
ALTER TABLE distilled_rules ADD COLUMN source_pattern TEXT;

-- 8. swarm_strategies: add auto-distillation metadata
ALTER TABLE swarm_strategies ADD COLUMN auto_distilled INTEGER DEFAULT 0;
ALTER TABLE swarm_strategies ADD COLUMN distilled_from_runs TEXT DEFAULT '[]';

-- 9. agent_profiles: add budget_limit for per-agent caps
ALTER TABLE agent_profiles ADD COLUMN token_budget_limit INTEGER;
