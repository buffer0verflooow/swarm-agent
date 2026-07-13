-- ============================================================================
-- Migration 011: Worker Signal Stream (Controller/Worker v0.7.0 Phase A)
--
-- 量化每个 Worker Agent 的产出质量，供 Controller 做 kill/boost/spawn 决策。
-- 设计原则:
--   1. 系统自动计算优先 (novelty_score, efficiency, loop_detected)
--   2. Agent 也可自报 (progress_marker, notes)
--   3. 每条 signal 关联工具调用或 capture 事件
-- ============================================================================

CREATE TABLE IF NOT EXISTS worker_signals (
    signal_id           TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    task_id             TEXT DEFAULT '',
    signal_type         TEXT NOT NULL
                        CHECK (signal_type IN (
                            'tool_output','finding','progress',
                            'loop_detect','heartbeat','error'
                        )),

    -- 产出质量: 这个信号的可信度/价值 (0-1)
    --   finding: 基于 capture 的 knowledge_type → 映射
    --   tool_output: 基于 content 是否含有用信号
    --   heartbeat: 基于 load_score
    output_quality      REAL NOT NULL DEFAULT 0.5,

    -- 新发现得分: 基于 content_hash 去重 + TF-IDF 相似度
    --   1.0 = 完全新发现
    --   0.0 = 完全重复之前的内容
    novelty_score       REAL NOT NULL DEFAULT 0.0,

    -- 产出/消耗比: (finding_count or 0) / tokens_spent_this_batch
    efficiency          REAL NOT NULL DEFAULT 0.0,

    -- 原地打转检测 (系统自动计算, 非 Agent 自报)
    --   连续 N 次 novelty_score < 0.1 → loop_detected=1
    loop_detected       INTEGER NOT NULL DEFAULT 0,

    -- 进度标记: Agent 自报的进度描述
    --   如: "scanned 8/20 endpoints", "verified 3/5 findings"
    progress_marker     TEXT DEFAULT '',

    -- 最后一次产生"有价值"输出的时间
    --   有价值: output_quality >= 0.6 OR novelty_score >= 0.3
    last_useful_at      TEXT,

    -- 原始数据引用
    raw_output_hash     TEXT DEFAULT '',
    raw_output_snippet  TEXT DEFAULT '',
    knowledge_entry_id  TEXT DEFAULT '',

    -- 底层 token 数据
    tokens_spent_since  INTEGER DEFAULT 0,
    findings_since      INTEGER DEFAULT 0,

    notes               TEXT DEFAULT '',
    metadata            TEXT DEFAULT '{}',
    created_at          TEXT DEFAULT (datetime('now'))
);

-- 按 run + agent 查询最近信号 (Controller 每 60s 审视)
CREATE INDEX IF NOT EXISTS idx_worker_signals_run_agent
    ON worker_signals(run_id, agent_id, created_at DESC);

-- 快速找高质量信号
CREATE INDEX IF NOT EXISTS idx_worker_signals_quality
    ON worker_signals(run_id, output_quality DESC, created_at DESC);

-- 找低质量/卡住的 Worker
CREATE INDEX IF NOT EXISTS idx_worker_signals_stuck
    ON worker_signals(run_id, agent_id, loop_detected, novelty_score);

-- 按 task_id 关联
CREATE INDEX IF NOT EXISTS idx_worker_signals_task
    ON worker_signals(task_id, created_at DESC);
