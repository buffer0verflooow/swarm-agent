-- ============================================================================
-- Swarm Extensions — Agent 生命周期 + 动态 Spawn
-- 
-- P1 of optimization plan: adds spawn_requests (stigmergy signal bus) 
-- and agent_heartbeats (liveness detection).
-- ============================================================================

-- Agent 请求生成新 Agent（stigmergy spawn 信号）
-- Agent 发现需要协作时写入此表，Orchestrator 轮询处理。
CREATE TABLE IF NOT EXISTS spawn_requests (
    request_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES swarm_runs(run_id) ON DELETE CASCADE,
    requesting_agent TEXT NOT NULL,           -- 谁请求
    parent_task_id  TEXT REFERENCES agent_tasks(task_id) ON DELETE SET NULL,
    requested_role  TEXT NOT NULL,            -- 需要什么角色 (scanner/analyst/exploiter/...)
    reason          TEXT NOT NULL,            -- 为什么需要（触发 spawn 的发现描述）
    context_entry_ids TEXT DEFAULT '[]',      -- 触发 spawn 的 knowledge_entry id 列表
    priority        INTEGER DEFAULT 50,       -- 优先级 0-100
    status          TEXT DEFAULT 'pending'
                    CHECK (status IN ('pending','spawning','fulfilled','rejected','expired')),
    spawned_agent_id TEXT REFERENCES agent_profiles(agent_id) ON DELETE SET NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    expires_at      TEXT DEFAULT (datetime('now', '+10 minutes'))
);
CREATE INDEX IF NOT EXISTS idx_sr_status  ON spawn_requests(status);
CREATE INDEX IF NOT EXISTS idx_sr_run     ON spawn_requests(run_id);
CREATE INDEX IF NOT EXISTS idx_sr_expires ON spawn_requests(expires_at);

-- Agent 心跳（存活检测）
-- Agent 每 30s 更新一次，Orchestrator 每 10s 扫描超时者。
CREATE TABLE IF NOT EXISTS agent_heartbeats (
    agent_id        TEXT PRIMARY KEY REFERENCES agent_profiles(agent_id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL REFERENCES swarm_runs(run_id) ON DELETE CASCADE,
    last_beat       TEXT DEFAULT (datetime('now')),
    beat_count      INTEGER DEFAULT 1,
    current_task_id TEXT REFERENCES agent_tasks(task_id) ON DELETE SET NULL,
    load_score      REAL DEFAULT 0.0,   -- 0~1，当前繁忙程度
    metadata        TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_hb_run  ON agent_heartbeats(run_id);
CREATE INDEX IF NOT EXISTS idx_hb_beat ON agent_heartbeats(last_beat);
