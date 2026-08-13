-- ============================================================================
-- Migration 016: research 产品线角色与技能 (2026-08-12)
--
-- 背景: company_router 的 research 路由 (竞品/调研/技术选型) 此前把 intent
-- 压成 analyze/report, 命中 task_skill_index 的二进制分析行 (analyst +
-- nm/objdump/readelf/ELF symver), 对市场/技术调研是语义错配。
--
-- 本次: 新增独立 task_type 'research' + 'researcher' 角色 + researcher 技能,
-- 与 security 产品线完全分离; research 任务不再注入二进制分析技能。
--
-- 幂等: INSERT OR IGNORE (种子只写一次)。
-- ============================================================================

INSERT OR IGNORE INTO task_skill_index (task_type, role, load_skills, tool_allowlist) VALUES
    ('research', 'researcher', '["researcher"]', '["curl","python3","rg"]');

INSERT OR IGNORE INTO model_profiles
    (profile_id, role, provider, model, priority, is_default, enabled, max_tokens,
     temperature, tool_policy, system_prompt, metadata, load_skills, tool_allowlist,
     mcp_servers, updated_at)
VALUES
    ('default-researcher-balanced', 'researcher', 'client', 'balanced', 80, 1, 1, 16000,
     0.3, '{"network": true, "shell": true, "write": false}',
     'You are a researcher worker. Collect multi-source evidence, cross-verify facts, distinguish fact/inference/uncertainty, and produce structured, citation-backed analysis.',
     '{}', '["researcher"]', '["curl","python3","rg"]', '[]', datetime('now'));
