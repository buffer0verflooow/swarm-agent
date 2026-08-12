-- ============================================================================
-- Migration 010: 任务→角色→技能 索引表 (2026-08-12)
--
-- 用户需求: 蜂群收到任务时, 根据索引表找到处理问题所需的技能、工具。
-- 机制: publish_work_task 入队时查 task_skill_index 推导 role + load_skills +
--       tool_allowlist, 写入任务行 focus_params (task_skills/task_tools);
--       worker build_task_context 将任务级技能与角色技能合并注入 (任务级优先)。
-- 语义: 静态查表 (用户选定: 确定性、可编辑、最小)。task_type → 角色+技能+工具。
--       未命中的 task_type 回退 ROLE_BY_TASK_TYPE (向后兼容, 旧行为不变)。
--
-- 表结构:
--   task_type     任务类型 (scan/analyze/exploit/report/custom, 可扩展)
--   role          推导出的执行角色
--   load_skills   JSON 数组: 技能引用 (skills/*.md 文件名, 可多个)
--   tool_allowlist JSON 数组: 可用工具 (注入上下文, 执行强制留后续)
--
-- 幂等: CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE (种子只写一次)。
-- 编辑: 直接 UPDATE task_skill_index ... WHERE task_type='...' 即可生效。
-- ============================================================================

CREATE TABLE IF NOT EXISTS task_skill_index (
    task_type      TEXT PRIMARY KEY,
    role           TEXT NOT NULL,
    load_skills    TEXT NOT NULL DEFAULT '[]',
    tool_allowlist TEXT NOT NULL DEFAULT '[]',
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 种子: 5 个默认任务类型 → 角色 + 技能 + 工具 (与 migration 008/009 的角色技能一致)
INSERT OR IGNORE INTO task_skill_index (task_type, role, load_skills, tool_allowlist) VALUES
    ('scan',    'scanner',   '["scanner"]',   '["curl","nmap","dig","file","strings"]'),
    ('analyze', 'analyst',   '["analyst"]',   '["file","strings","nm","objdump","readelf"]'),
    ('exploit', 'exploiter', '["exploiter"]', '["gcc","python3","gdb","objdump","readelf","xxd"]'),
    ('report',  'reporter',  '["reporter"]',  '[]'),
    ('custom',  'custom',    '["custom"]',    '[]');
