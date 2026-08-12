-- ============================================================================
-- Migration 009: 蜂群技能/MCP 自主化 (2026-08-12)
--
-- 1. load_skills 从"方法论句子"改为真实技能文件名 (skills/*.md)。
--    2026-08-11 ablation 结论: 注入管线可用但方法论句子零增益; 机制需要
--    真实内容文件(领域事实型)。migration 009 让默认 profile 指向
--    skills/{scanner,analyst,exploiter,reporter,custom}.md。
--    旧条目(句子)仍受支持: resolver 解析不到技能文件时按原样透传。
--
-- 2. model_profiles 增加 mcp_servers 列 (JSON 数组): 角色可用的 MCP
--    服务器名, 指向 mcp_servers.json。worker 上下文按此注入 MCP 工具段,
--    工具调用走 scripts/mcp_tool.py (蜂群自持 stdio 客户端, 不依赖
--    Hermes 的 config.yaml mcp_servers)。
--
-- 幂等: ALTER 重复列自动跳过 (db.init 语义); UPDATE 结果幂等。
-- ============================================================================

ALTER TABLE model_profiles ADD COLUMN mcp_servers TEXT DEFAULT '[]';

UPDATE model_profiles SET load_skills = '["scanner"]' WHERE role = 'scanner' AND is_default = 1;
UPDATE model_profiles SET load_skills = '["analyst"]' WHERE role = 'analyst' AND is_default = 1;
UPDATE model_profiles SET load_skills = '["exploiter"]' WHERE role = 'exploiter' AND is_default = 1;
UPDATE model_profiles SET load_skills = '["reporter"]' WHERE role = 'reporter' AND is_default = 1;
UPDATE model_profiles SET load_skills = '["custom"]' WHERE role = 'custom' AND is_default = 1;
