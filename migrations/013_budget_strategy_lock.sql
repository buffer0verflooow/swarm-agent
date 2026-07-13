-- Migration 013: Add optimistic lock on budget_strategy writes
-- Both Controller and PowerSchedule can write to swarm_runs.budget_strategy.
-- strategy_version provides optimistic concurrency control.
ALTER TABLE swarm_runs ADD COLUMN strategy_version INTEGER DEFAULT 0;
