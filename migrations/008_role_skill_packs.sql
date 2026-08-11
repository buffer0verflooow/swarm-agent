-- ============================================================================
-- Migration 008: 角色技能包 (第 1 层修正 - 角色→技能映射)
--
-- 背景: 蜂群"角色固定、工具统一"——所有 agent 同一套工具, 角色只做 claim
-- 过滤 + prompt 提示, 没有 reverselibrary 式 _SUBAGENT_SKILLS 映射。
-- 第 1 层: model_profiles 增加 load_skills(技能提示, JSON 数组)与
-- tool_allowlist(工具白名单, JSON 数组), worker 按 role 注入。
--
-- 评估语义 (2026-08-11 ablation): load_skills 注入 executor prompt 上下文,
-- 用 pwncollege what-is-a-bug 挑战做基线 vs +技能包 对照。
-- ============================================================================

ALTER TABLE model_profiles ADD COLUMN load_skills TEXT DEFAULT '[]';
ALTER TABLE model_profiles ADD COLUMN tool_allowlist TEXT DEFAULT '[]';

-- 预置角色技能包 (面向二进制/漏洞挖掘场景)
UPDATE model_profiles SET
    load_skills = '[
        "探测优先: 广度优先收集目标信息, 记录端口/服务/版本",
        "每条发现附证据: 命令原文 + 输出特征, 不写无依据的 impact",
        "识别攻击面: 输入入口/解析器/反序列化点/权限边界"
    ]',
    tool_allowlist = '["curl", "nmap", "dig", "file", "strings"]'
WHERE role = 'scanner';

UPDATE model_profiles SET
    load_skills = '[
        "静态分析: 先看数据流/边界检查/类型转换, 再下漏洞结论",
        "机理优先: 用具体代码路径证明漏洞, 拒绝仅凭函数名/训练先验下结论",
        "证据分级: 工具输出 > 交叉引用 > 语义联想; 无法验证的标注不确定性",
        "反例意识: 主动寻找缓解因素 (canary/PIE/长度检查/白名单)"
    ]',
    tool_allowlist = '["file", "strings", "nm", "objdump", "readelf"]'
WHERE role = 'analyst';

UPDATE model_profiles SET
    load_skills = '[
        "构造优先: 最小 PoC 优先, 逐步加复杂特征",
        "字节级精度: 手工二进制/协议构造注意对齐、偏移、大小端",
        "失败反馈闭环: 每次执行失败先诊断 (退出码/stderr/崩溃点) 再改, 不盲目重试",
        "语言意识: 输出脚本语言与目标语言严格区分, 模板/转义逐字核对"
    ]',
    tool_allowlist = '["gcc", "python3", "gdb", "objdump", "readelf", "xxd"]'
WHERE role = 'exploiter';

UPDATE model_profiles SET
    load_skills = '[
        "只汇总已验证证据, 区分事实/推断/存疑",
        "影响评估绑定证据链: 每个 impact 附复现依据",
        "输出含: 结论 / 证据 / 不确定性 / 修复建议"
    ]',
    tool_allowlist = '[]'
WHERE role = 'reporter';

UPDATE model_profiles SET
    load_skills = '[
        "先读任务上下文与知识库相关条目, 再动手",
        "结论须可验证: 附命令/URL/代码路径作为证据"
    ]',
    tool_allowlist = '[]'
WHERE role = 'custom';
