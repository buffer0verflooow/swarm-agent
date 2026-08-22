-- ============================================================================
-- Migration 020: 模型对照表 — 免费池分层 (token 成本优化, 2026-08-22)
--
-- 目标: 蜂群批量角色优先使用免费模型 (OpenCode Zen / ZenMux free),
--       按日限额轮询, 超限自动降级到付费模型。
--
-- 1. model_profiles 增加 tier 列: 'free' (免费池候选) / 'paid' (默认付费)。
-- 2. model_usage_daily 表: 按 (model_key, date) 记录 token 用量与调用次数,
--    免费池轮询与限额判断的数据源。
-- 3. 种子数据: 批量角色 (scanner/data-analyst/reporter/report-writer/
--    content-writer/custom) 各插一条 tier='free' 的 profile 行, 指向
--    opencode 可调用的免费模型 (executor 侧用 `opencode run --model` 执行)。
--
-- 免费池模型 id 采用 opencode 命名空间 (provider/model), 与
-- ~/.config/opencode/opencode.jsonc 的 provider 配置一一对应。
-- 幂等: ALTER 幂等由 src/db.py _ensure_model_tier_schema 兜底; 种子用
-- INSERT OR IGNORE。
-- ============================================================================

-- 1. model_profiles.tier 列 (默认 paid; 已有行全部落 paid, 行为不变)
ALTER TABLE model_profiles ADD COLUMN tier TEXT NOT NULL DEFAULT 'paid'
    CHECK (tier IN ('free', 'paid'));

-- 2. 免费模型每日用量表
CREATE TABLE IF NOT EXISTS model_usage_daily (
    model_key  TEXT NOT NULL,   -- 免费池模型标识, 如 'zenmux/z-ai/glm-5.3-free'
    usage_date TEXT NOT NULL,   -- 'YYYY-MM-DD' (本地日期)
    tokens     INTEGER NOT NULL DEFAULT 0,  -- 当日累计 token (尽力上报, 可为 0)
    calls      INTEGER NOT NULL DEFAULT 0,  -- 当日累计调用次数
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (model_key, usage_date)
);

-- 3. 免费池种子: 批量角色 → 免费模型 (priority 越高越优先; is_default=0
--    不影响现有付费默认 profile 语义 — 免费池是 tier 维度, 不是默认 profile)
INSERT OR IGNORE INTO model_profiles
    (profile_id, role, provider, model, priority, is_default, enabled, max_tokens,
     temperature, tool_policy, system_prompt, metadata, load_skills, tool_allowlist,
     mcp_servers, updated_at, tier)
VALUES
    ('free-scanner-glm53', 'scanner', 'zenmux', 'z-ai/glm-5.3-free',
     90, 0, 1, 8000, 0.2, '{}', '',
     '{"free_pool": true, "daily_limit_tokens": 1000000, "daily_limit_calls": 300}',
     '[]', '[]', '[]', datetime('now'), 'free'),
    ('free-data-analyst-glm53', 'data-analyst', 'zenmux', 'z-ai/glm-5.3-free',
     90, 0, 1, 8000, 0.2, '{}', '',
     '{"free_pool": true, "daily_limit_tokens": 1000000, "daily_limit_calls": 300}',
     '[]', '[]', '[]', datetime('now'), 'free'),
    ('free-reporter-nemotron', 'reporter', 'opencode', 'nemotron-3-ultra-free',
     90, 0, 1, 8000, 0.2, '{}', '',
     '{"free_pool": true, "daily_limit_tokens": 1000000, "daily_limit_calls": 300}',
     '[]', '[]', '[]', datetime('now'), 'free'),
    ('free-report-writer-nemotron', 'report-writer', 'opencode', 'nemotron-3-ultra-free',
     90, 0, 1, 8000, 0.2, '{}', '',
     '{"free_pool": true, "daily_limit_tokens": 1000000, "daily_limit_calls": 300}',
     '[]', '[]', '[]', datetime('now'), 'free'),
    ('free-content-writer-nemotron', 'content-writer', 'opencode', 'nemotron-3-ultra-free',
     90, 0, 1, 8000, 0.2, '{}', '',
     '{"free_pool": true, "daily_limit_tokens": 1000000, "daily_limit_calls": 300}',
     '[]', '[]', '[]', datetime('now'), 'free'),
    ('free-custom-glm53', 'custom', 'zenmux', 'z-ai/glm-5.3-free',
     90, 0, 1, 8000, 0.2, '{}', '',
     '{"free_pool": true, "daily_limit_tokens": 1000000, "daily_limit_calls": 300}',
     '[]', '[]', '[]', datetime('now'), 'free');
