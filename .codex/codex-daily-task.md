# Swarm Controller/Worker P0/P1 Fixes

Apply the following P0 and P1 fixes. Work through each fix, verify it compiles, then move to the next.

## Fix 1 (P0): Switch controller to LLM mode
File: src/swarm/orchestrator.py, line 555
Change `mode="rules"` to `mode="llm"` so the Controller uses LLM-driven decisions (the architecture design intent). The Controller already has built-in fallback to rules if LLM fails, so this is safe.

## Fix 2 (P0): Add optimistic lock on budget_strategy
Both controller.py and orchestrator.py write to swarm_runs.budget_strategy, risking race conditions.

Step A: Add strategy_version column if missing:
```sql
ALTER TABLE swarm_runs ADD COLUMN strategy_version INTEGER DEFAULT 0;
```

Step B: In controller.py _execute_adjust_budget() (around line 455-463), change the UPDATE to use optimistic locking with strategy_version. Fetch current version first, then UPDATE with WHERE strategy_version = current_version, and increment it. Retry once on failure.

Step C: In orchestrator.py _tick_power_schedule() (around line 520-524), apply the same optimistic lock pattern.

## Fix 3 (P0): Wrap _execute_kill() in explicit transaction
File: src/swarm/controller.py, _execute_kill() method
Wrap all execute() calls and the commit() in BEGIN IMMEDIATE / try-commit / except-rollback for atomicity.

## Fix 4 (P1): Upgrade compute_novelty_score() to MinHash
File: src/swarm/signals.py, compute_novelty_score() function
Replace the crude token-overlap approach (set intersection on first 100 tokens) with MinHash-based Jaccard similarity. Implement a simple MinHash using Python's built-in hashlib with 128 hash functions and 3-gram word shingles. Keep the content_hash duplicate check at the top.

## Fix 5 (P1): Add health check tick to orchestrator
File: src/swarm/orchestrator.py
Add POLL_HEALTH_SEC = 30 constant. Add _tick_health() method that checks:
- Key sub-modules can be imported (signals, controller, exploration, governance)
- worker_signals table hasn't exceeded 100K rows
Log warnings for issues, debug log when all OK.
Wire it into run_loop with last_health tracking.

## Fix 6 (P1): Spawn scanner with context after all workers killed
File: src/swarm/controller.py, _tick_rules() and _execute_spawn()
When spawning a new worker because too few remain, include context_entry_ids from recent knowledge entries so the new scanner has direction. Also update _execute_spawn() to pass context_entry_ids to request_spawn().

## Verification
After all fixes, run:
```bash
cd ~/workspace/research/swarm-knowledge
python3 -m pytest tests/test_controller.py tests/test_worker_signals.py tests/test_exploration_traces.py tests/test_worker_mode.py -x -q 2>&1
```
Fix any failures.
