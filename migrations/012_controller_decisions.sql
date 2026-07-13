-- ============================================================================
-- Migration 012: Controller Decision Audit (Controller/Worker v0.7.0 Phase B)
--
-- 记录 Controller LLM 的每次判决，支持审计和回溯。
-- 每个决策可以被执行、回滚、或标记为误判（用于 Controller 自我改进）。
-- ============================================================================

CREATE TABLE IF NOT EXISTS controller_decisions (
    decision_id         TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    tick_number         INTEGER DEFAULT 0,
    -- 输入快照
    budget_remaining    REAL DEFAULT 0,
    budget_strategy     TEXT DEFAULT '',
    active_workers      INTEGER DEFAULT 0,
    stuck_workers       INTEGER DEFAULT 0,
    -- 判决详情
    decision_type       TEXT NOT NULL
                        CHECK (decision_type IN (
                            'kill', 'boost', 'spawn', 'redirect',
                            'noop', 'adjust_budget'
                        )),
    target_agent_id     TEXT DEFAULT '',
    target_role         TEXT DEFAULT '',
    reason              TEXT NOT NULL,
    confidence          REAL DEFAULT 0.7,
    -- 执行状态
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending','executed','failed','rolled_back',
                            'false_positive','skipped'
                        )),
    executed_at         TEXT,
    result_summary      TEXT DEFAULT '',
    -- LLM 元数据
    llm_model           TEXT DEFAULT '',
    llm_input_tokens    INTEGER DEFAULT 0,
    llm_output_tokens   INTEGER DEFAULT 0,
    llm_raw_response    TEXT DEFAULT '',
    -- 审计
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_controller_decisions_run
    ON controller_decisions(run_id, tick_number DESC);

CREATE INDEX IF NOT EXISTS idx_controller_decisions_type
    ON controller_decisions(run_id, decision_type, status);

CREATE INDEX IF NOT EXISTS idx_controller_decisions_target
    ON controller_decisions(target_agent_id, created_at DESC);
