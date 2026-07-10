-- ============================================================================
-- Migration 010: Exploration Traces (Phase A — 粗粒度探索追踪)
--
-- 记录每次 Agent 测试行为，包括"没发现漏洞"的测试。
-- Phase A: 不做语义归一化，不自动阻止任务生成，只提供信息供 Agent 判断。
--
-- 设计原则:
--   1. 粗粒度 — 记录 literal target_url，不做路径模板化
--   2. 信息型 — 不阻止任务生成，Agent 自行决定是否重复测试
--   3. 可审计 — 每条记录关联 run_id + task_id + agent_id
--   4. 跨 run 复用 — 同一 target 的多次 run 共享探索历史
-- ============================================================================

CREATE TABLE IF NOT EXISTS exploration_traces (
    trace_id            TEXT PRIMARY KEY,
    run_id              TEXT DEFAULT '',
    task_id             TEXT DEFAULT '',
    agent_id            TEXT DEFAULT '',
    target_url          TEXT NOT NULL,
    method              TEXT NOT NULL DEFAULT 'GET',
    vulnerability_class TEXT NOT NULL,
    result              TEXT NOT NULL
                        CHECK (result IN (
                            'found','not_found','blocked','error','inconclusive'
                        )),
    finding_id          TEXT DEFAULT '',
    depth               TEXT NOT NULL DEFAULT 'shallow'
                        CHECK (depth IN ('shallow','medium','deep')),
    notes               TEXT DEFAULT '',
    metadata            TEXT DEFAULT '{}',
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_exploration_traces_run
    ON exploration_traces(run_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_exploration_traces_target
    ON exploration_traces(target_url, vulnerability_class);

CREATE INDEX IF NOT EXISTS idx_exploration_traces_task
    ON exploration_traces(task_id);

-- 用于快速查询: 某个 (target_url × vuln_class) 是否已被测试
CREATE INDEX IF NOT EXISTS idx_exploration_traces_coverage
    ON exploration_traces(target_url, vulnerability_class, result);

-- 跨 run 视图: 同一 target 在所有 run 中的探索状态
CREATE INDEX IF NOT EXISTS idx_exploration_traces_target_all
    ON exploration_traces(target_url, vulnerability_class, result, depth, created_at DESC);
