-- ============================================================================
-- Swarm Knowledge Base — SQLite Schema (Single File)
-- 
-- 所有表在一个 SQLite 文件中，零配置，拷贝即分享。
-- 类型适配: UUID→TEXT, JSONB→TEXT, ARRAY→TEXT(JSON), TIMESTAMPTZ→TEXT
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================================
-- 1. Swarm Core — 蜂群运行记录
-- ============================================================================

CREATE TABLE IF NOT EXISTS swarm_runs (
    run_id          TEXT PRIMARY KEY,
    swarm_name      TEXT NOT NULL,
    intent          TEXT NOT NULL CHECK (intent IN ('recon','exploit','analyze','defend','report','custom')),
    target_type     TEXT NOT NULL CHECK (target_type IN ('ip','binary','apk','webapp','domain','network','unknown')),
    target_id       TEXT NOT NULL,
    status          TEXT DEFAULT 'running' CHECK (status IN ('pending','running','completed','failed','cancelled')),
    config          TEXT DEFAULT '{}',
    stats           TEXT DEFAULT '{}',
    started_at      TEXT DEFAULT (datetime('now')),
    ended_at        TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_swarm_runs_swarm   ON swarm_runs(swarm_name);
CREATE INDEX idx_swarm_runs_intent  ON swarm_runs(intent);
CREATE INDEX idx_swarm_runs_status  ON swarm_runs(status);

CREATE TABLE IF NOT EXISTS agent_profiles (
    agent_id        TEXT PRIMARY KEY,
    agent_name      TEXT UNIQUE NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('scanner','analyst','exploiter','reporter','orchestrator','custom')),
    capabilities    TEXT DEFAULT '[]',
    default_tools   TEXT DEFAULT '[]',
    model_preference TEXT,
    status          TEXT DEFAULT 'active' CHECK (status IN ('active','idle','deprecated')),
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id         TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES swarm_runs(run_id) ON DELETE CASCADE,
    agent_id        TEXT REFERENCES agent_profiles(agent_id) ON DELETE SET NULL,
    parent_task_id  TEXT REFERENCES agent_tasks(task_id) ON DELETE SET NULL,
    task_type       TEXT NOT NULL CHECK (task_type IN ('scan','analyze','exploit','report','subtask','custom')),
    task_intent     TEXT,
    focus_params    TEXT DEFAULT '{}',
    iteration       INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','timeout')),
    result_summary  TEXT DEFAULT '{}',
    token_cost      INTEGER DEFAULT 0,
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_agent_tasks_run     ON agent_tasks(run_id);
CREATE INDEX idx_agent_tasks_agent   ON agent_tasks(agent_id);
CREATE INDEX idx_agent_tasks_parent  ON agent_tasks(parent_task_id);
CREATE INDEX idx_agent_tasks_status  ON agent_tasks(status);

CREATE TABLE IF NOT EXISTS agent_delegations (
    delegation_id   TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    from_agent_id   TEXT REFERENCES agent_profiles(agent_id) ON DELETE SET NULL,
    to_agent_id     TEXT REFERENCES agent_profiles(agent_id) ON DELETE SET NULL,
    request_id      TEXT NOT NULL UNIQUE,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','timeout')),
    duration_ms     INTEGER,
    token_cost      INTEGER DEFAULT 0,
    error_code      TEXT,
    error_message   TEXT,
    retry_count     INTEGER DEFAULT 0,
    context_summary TEXT DEFAULT '{}',
    result_summary  TEXT DEFAULT '{}',
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_delegations_task    ON agent_delegations(task_id);
CREATE INDEX idx_delegations_status  ON agent_delegations(status);

CREATE TABLE IF NOT EXISTS swarm_behaviors (
    behavior_id     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES swarm_runs(run_id) ON DELETE CASCADE,
    behavior_type   TEXT NOT NULL CHECK (behavior_type IN ('emergence','collaboration','conflict','adaptation','optimization')),
    description     TEXT NOT NULL,
    trigger_agents  TEXT NOT NULL DEFAULT '[]',
    outcome         TEXT,
    significance    REAL DEFAULT 0.5 CHECK (significance >= 0 AND significance <= 1),
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_behaviors_run  ON swarm_behaviors(run_id);
CREATE INDEX idx_behaviors_type ON swarm_behaviors(behavior_type);

-- ============================================================================
-- 2. Knowledge Core — DIKW 知识金字塔
-- ============================================================================

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id                  TEXT PRIMARY KEY,
    level               INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
    knowledge_type      TEXT NOT NULL CHECK (knowledge_type IN (
                            'observation','fact','mechanism','vulnerability',
                            'technique','pattern','reference','strategy',
                            'heuristic','counter_example','tool_usage'
                        )),
    content             TEXT NOT NULL,
    title               TEXT,
    source_agent        TEXT NOT NULL,
    source_run_id       TEXT REFERENCES swarm_runs(run_id) ON DELETE SET NULL,
    source_task_id      TEXT REFERENCES agent_tasks(task_id) ON DELETE SET NULL,
    domain              TEXT,
    subdomain           TEXT,
    knowledge_intent    TEXT CHECK (knowledge_intent IN ('understand','attack','defend','enumerate','optimize')),
    trust_vector        TEXT DEFAULT '{"logic_soundness":0.7,"base_confidence":0.6,"cross_validation":0.0}',
    status              TEXT DEFAULT 'active' CHECK (status IN ('active','stale','invalidated','superseded')),
    superseded_by       TEXT REFERENCES knowledge_entries(id) ON DELETE SET NULL,
    promoted_by         TEXT,
    promoted_at         TEXT,
    tags                TEXT DEFAULT '[]',
    scratchpad_id       TEXT,
    -- Clustering
    cluster_id          TEXT,
    is_cluster_centroid INTEGER DEFAULT 0,
    cluster_updated_at  TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_ke_level        ON knowledge_entries(level);
CREATE INDEX idx_ke_status       ON knowledge_entries(status);
CREATE INDEX idx_ke_type         ON knowledge_entries(knowledge_type);
CREATE INDEX idx_ke_source_agent ON knowledge_entries(source_agent);
CREATE INDEX idx_ke_source_run   ON knowledge_entries(source_run_id);
CREATE INDEX idx_ke_domain       ON knowledge_entries(domain);
CREATE INDEX idx_ke_intent       ON knowledge_entries(knowledge_intent);
CREATE INDEX idx_ke_created      ON knowledge_entries(created_at DESC);
CREATE INDEX idx_ke_cluster      ON knowledge_entries(cluster_id);

-- FTS5 for full-text search on knowledge entries
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_entries_fts USING fts5(
    title, content, content=knowledge_entries, content_rowid=rowid
);

CREATE TABLE IF NOT EXISTS knowledge_lineage (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id            TEXT NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
    source_type             TEXT NOT NULL CHECK (source_type IN (
                                'agent_execution','agent_inference','cross_agent_validation',
                                'human_feedback','document_extraction','ontology_inference',
                                'swarm_emergence'
                            )),
    source_ref              TEXT NOT NULL DEFAULT '{}',
    extraction_method       TEXT NOT NULL CHECK (extraction_method IN (
                                'agent_analysis','llm_extraction','pattern_matching',
                                'cross_validation','human','swarm_consensus'
                            )),
    confidence_contribution REAL NOT NULL DEFAULT 1.0 CHECK (confidence_contribution BETWEEN 0 AND 1),
    created_at              TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_kl_unique      ON knowledge_lineage(knowledge_id, source_type, source_ref);
CREATE INDEX idx_kl_knowledge          ON knowledge_lineage(knowledge_id);
CREATE INDEX idx_kl_source_type        ON knowledge_lineage(source_type);

CREATE TABLE IF NOT EXISTS knowledge_promotions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id            TEXT NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
    from_level              INTEGER NOT NULL,
    to_level                INTEGER NOT NULL,
    promoted_by             TEXT NOT NULL,
    reason                  TEXT NOT NULL,
    evidence_summary        TEXT,
    corroborating_sources   TEXT DEFAULT '[]',
    created_at              TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_kp_knowledge ON knowledge_promotions(knowledge_id);

CREATE TABLE IF NOT EXISTS distilled_rules (
    id                  TEXT PRIMARY KEY,
    rule_name           TEXT NOT NULL,
    rule_description    TEXT NOT NULL,
    rule_type           TEXT NOT NULL CHECK (rule_type IN ('strategy','heuristic','constraint','best_practice')),
    trigger_condition   TEXT DEFAULT '{}',
    rule_body           TEXT NOT NULL,
    source_knowledge_ids TEXT NOT NULL DEFAULT '[]',
    distilled_by        TEXT DEFAULT 'governance-engine',
    applicable_agents   TEXT DEFAULT '[]',
    priority            INTEGER DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    counter_example_count INTEGER DEFAULT 0,
    is_active           INTEGER DEFAULT 1,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_dr_type      ON distilled_rules(rule_type);
CREATE INDEX idx_dr_priority  ON distilled_rules(priority DESC);
CREATE INDEX idx_dr_active    ON distilled_rules(is_active);

CREATE TABLE IF NOT EXISTS counter_examples (
    id                  TEXT PRIMARY KEY,
    knowledge_id        TEXT NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
    source_run_id       TEXT REFERENCES swarm_runs(run_id) ON DELETE SET NULL,
    source_agent        TEXT NOT NULL,
    description         TEXT NOT NULL,
    evidence            TEXT,
    severity            TEXT DEFAULT 'moderate' CHECK (severity IN ('minor','moderate','major','fatal')),
    created_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_ce_knowledge ON counter_examples(knowledge_id);
CREATE INDEX idx_ce_severity  ON counter_examples(severity);

-- ============================================================================
-- 3. Ontology — 本体模型
-- ============================================================================

CREATE TABLE IF NOT EXISTS ontology_concepts (
    concept_id          TEXT PRIMARY KEY,
    concept_name        TEXT UNIQUE NOT NULL,
    concept_type        TEXT NOT NULL CHECK (concept_type IN (
                            'tool','technique','vulnerability','target_type',
                            'agent_role','task_type','behavior','domain',
                            'attack_stage','defense_stage','abstract'
                        )),
    description         TEXT,
    parent_concept_id   TEXT REFERENCES ontology_concepts(concept_id) ON DELETE SET NULL,
    properties          TEXT DEFAULT '{}',
    source              TEXT DEFAULT 'manual' CHECK (source IN ('manual','agent_discovered','extracted','inferred')),
    is_abstract         INTEGER DEFAULT 0,
    stability           REAL DEFAULT 0.5 CHECK (stability >= 0 AND stability <= 1),
    version             INTEGER DEFAULT 1,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_oc_type      ON ontology_concepts(concept_type);
CREATE INDEX idx_oc_parent    ON ontology_concepts(parent_concept_id);
CREATE INDEX idx_oc_stability ON ontology_concepts(stability);

CREATE TABLE IF NOT EXISTS ontology_relations (
    relation_id         TEXT PRIMARY KEY,
    from_concept_id     TEXT NOT NULL REFERENCES ontology_concepts(concept_id) ON DELETE CASCADE,
    to_concept_id       TEXT NOT NULL REFERENCES ontology_concepts(concept_id) ON DELETE CASCADE,
    relation_type       TEXT NOT NULL CHECK (relation_type IN (
                            'specializes','generalizes','uses','produces',
                            'depends_on','conflicts_with','mitigates','exploits',
                            'implements','triggers','prevents','evolves_to',
                            'composes','part_of','equivalent_to'
                        )),
    weight              REAL DEFAULT 1.0,
    confidence          REAL DEFAULT 0.8 CHECK (confidence >= 0 AND confidence <= 1),
    evidence            TEXT,
    bidirectional       INTEGER DEFAULT 0,
    source              TEXT DEFAULT 'manual' CHECK (source IN ('manual','agent_discovered','inferred','cross_validated')),
    source_knowledge_id TEXT REFERENCES knowledge_entries(id) ON DELETE SET NULL,
    source_run_id       TEXT REFERENCES swarm_runs(run_id) ON DELETE SET NULL,
    created_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(from_concept_id, to_concept_id, relation_type)
);
CREATE INDEX idx_or_from       ON ontology_relations(from_concept_id);
CREATE INDEX idx_or_to         ON ontology_relations(to_concept_id);
CREATE INDEX idx_or_type       ON ontology_relations(relation_type);
CREATE INDEX idx_or_confidence ON ontology_relations(confidence);

CREATE TABLE IF NOT EXISTS ontology_instances (
    instance_id         TEXT PRIMARY KEY,
    concept_id          TEXT NOT NULL REFERENCES ontology_concepts(concept_id) ON DELETE CASCADE,
    instance_name       TEXT NOT NULL,
    instance_value      TEXT DEFAULT '{}',
    source_run_id       TEXT REFERENCES swarm_runs(run_id) ON DELETE SET NULL,
    source_task_id      TEXT REFERENCES agent_tasks(task_id) ON DELETE SET NULL,
    source_agent        TEXT,
    occurrence_count    INTEGER DEFAULT 1,
    success_rate        REAL,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(concept_id, instance_name)
);
CREATE INDEX idx_oi_concept ON ontology_instances(concept_id);

CREATE TABLE IF NOT EXISTS concept_versions (
    version_id          TEXT PRIMARY KEY,
    concept_id          TEXT NOT NULL REFERENCES ontology_concepts(concept_id) ON DELETE CASCADE,
    version             INTEGER NOT NULL,
    snapshot            TEXT NOT NULL,
    change_description  TEXT,
    change_type         TEXT CHECK (change_type IN ('add_property','remove_property','rename','split','merge','refine','deprecate')),
    changed_by          TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(concept_id, version)
);
CREATE INDEX idx_cv_concept ON concept_versions(concept_id);

-- ============================================================================
-- 4. Swarm Strategies — 蜂群策略库
-- ============================================================================

CREATE TABLE IF NOT EXISTS swarm_strategies (
    strategy_id         TEXT PRIMARY KEY,
    strategy_name       TEXT UNIQUE NOT NULL,
    description         TEXT,
    strategy_type       TEXT NOT NULL CHECK (strategy_type IN (
                            'task_decomposition','agent_selection','parallelization',
                            'escalation','fallback','optimization','coordination'
                        )),
    strategy_body       TEXT NOT NULL,
    trigger_intent      TEXT,
    trigger_target_type TEXT,
    trigger_complexity  TEXT CHECK (trigger_complexity IN ('simple','medium','complex')),
    source_knowledge_ids TEXT DEFAULT '[]',
    distilled_by        TEXT,
    use_count           INTEGER DEFAULT 0,
    success_count       INTEGER DEFAULT 0,
    avg_duration_ms     INTEGER,
    is_active           INTEGER DEFAULT 1,
    priority            INTEGER DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_ss_type     ON swarm_strategies(strategy_type);
CREATE INDEX idx_ss_intent   ON swarm_strategies(trigger_intent);
CREATE INDEX idx_ss_priority ON swarm_strategies(priority DESC);
CREATE INDEX idx_ss_active   ON swarm_strategies(is_active);

CREATE TABLE IF NOT EXISTS strategy_applications (
    application_id      TEXT PRIMARY KEY,
    strategy_id         TEXT NOT NULL REFERENCES swarm_strategies(strategy_id) ON DELETE CASCADE,
    run_id              TEXT NOT NULL REFERENCES swarm_runs(run_id) ON DELETE CASCADE,
    applied_by          TEXT NOT NULL,
    outcome             TEXT NOT NULL CHECK (outcome IN ('success','partial','failure')),
    notes               TEXT,
    duration_ms         INTEGER,
    created_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_sa_strategy ON strategy_applications(strategy_id);
CREATE INDEX idx_sa_run      ON strategy_applications(run_id);
CREATE INDEX idx_sa_outcome  ON strategy_applications(outcome);

-- ============================================================================
-- 5. Seed Data — 种子本体
-- ============================================================================

-- Agent Roles
INSERT OR IGNORE INTO ontology_concepts (concept_id, concept_name, concept_type, description, source, is_abstract, stability) VALUES
    ('seed-agent-role', 'agent_role', 'abstract', 'Agent 角色抽象概念', 'manual', 1, 1.0),
    ('seed-scanner', 'scanner', 'agent_role', '扫描型 Agent', 'manual', 0, 1.0),
    ('seed-analyst', 'analyst', 'agent_role', '分析型 Agent', 'manual', 0, 1.0),
    ('seed-exploiter', 'exploiter', 'agent_role', '利用型 Agent', 'manual', 0, 1.0),
    ('seed-reporter', 'reporter', 'agent_role', '报告型 Agent', 'manual', 0, 1.0),
    ('seed-orchestrator', 'orchestrator', 'agent_role', '编排型 Agent', 'manual', 0, 1.0);

-- Tools
INSERT OR IGNORE INTO ontology_concepts (concept_id, concept_name, concept_type, description, source, is_abstract) VALUES
    ('seed-tool-abstract', 'tool', 'abstract', '工具抽象概念', 'manual', 1),
    ('seed-nmap', 'nmap', 'tool', '网络扫描器', 'manual', 0),
    ('seed-nuclei', 'nuclei', 'tool', '漏洞扫描器', 'manual', 0),
    ('seed-sqlmap', 'sqlmap', 'tool', 'SQL 注入工具', 'manual', 0),
    ('seed-metasploit', 'metasploit', 'tool', '渗透测试框架', 'manual', 0),
    ('seed-burpsuite', 'burpsuite', 'tool', 'Web 安全测试平台', 'manual', 0),
    ('seed-hashcat', 'hashcat', 'tool', '密码破解', 'manual', 0),
    ('seed-ffuf', 'ffuf', 'tool', 'Web 模糊测试', 'manual', 0),
    ('seed-ghidra', 'ghidra', 'tool', '逆向工程框架', 'manual', 0),
    ('seed-jadx', 'jadx', 'tool', 'APK 反编译器', 'manual', 0),
    ('seed-frida', 'frida', 'tool', '动态插桩框架', 'manual', 0),
    ('seed-ida-pro', 'ida_pro', 'tool', '交互式反汇编器', 'manual', 0),
    ('seed-radare2', 'radare2', 'tool', 'CLI 逆向框架', 'manual', 0),
    ('seed-bloodhound', 'bloodhound', 'tool', 'AD 攻击路径分析', 'manual', 0),
    ('seed-impacket', 'impacket', 'tool', '网络协议工具集', 'manual', 0);

-- Techniques
INSERT OR IGNORE INTO ontology_concepts (concept_id, concept_name, concept_type, description, source, is_abstract) VALUES
    ('seed-tech-abstract', 'attack_technique', 'abstract', '攻击技术抽象', 'manual', 1),
    ('seed-port-scan', 'port_scan', 'technique', '端口扫描', 'manual', 0),
    ('seed-vuln-scan', 'vuln_scan', 'technique', '漏洞扫描', 'manual', 0),
    ('seed-sqli', 'sql_injection', 'technique', 'SQL 注入', 'manual', 0),
    ('seed-xss', 'xss', 'technique', '跨站脚本攻击', 'manual', 0),
    ('seed-privesc', 'privilege_escalation', 'technique', '权限提升', 'manual', 0),
    ('seed-lateral', 'lateral_movement', 'technique', '横向移动', 'manual', 0),
    ('seed-reverse', 'reverse_engineering', 'technique', '逆向工程', 'manual', 0),
    ('seed-dynamic', 'dynamic_analysis', 'technique', '动态分析', 'manual', 0),
    ('seed-static', 'static_analysis', 'technique', '静态分析', 'manual', 0),
    ('seed-fuzzing', 'fuzzing', 'technique', '模糊测试', 'manual', 0),
    ('seed-code-review', 'code_review', 'technique', '代码审计', 'manual', 0),
    ('seed-osint', 'osint', 'technique', '开源情报', 'manual', 0),
    ('seed-cred-extract', 'credential_extraction', 'technique', '凭证提取', 'manual', 0),
    ('seed-c2', 'c2_communication', 'technique', 'C2 通信', 'manual', 0);

-- Vulnerabilities
INSERT OR IGNORE INTO ontology_concepts (concept_id, concept_name, concept_type, description, source, is_abstract) VALUES
    ('seed-vuln-abstract', 'vulnerability', 'abstract', '漏洞抽象概念', 'manual', 1),
    ('seed-injection', 'injection', 'vulnerability', '注入类漏洞', 'manual', 1),
    ('seed-broken-auth', 'broken_auth', 'vulnerability', '认证失效', 'manual', 0),
    ('seed-data-exposure', 'sensitive_data_exposure', 'vulnerability', '敏感数据泄露', 'manual', 0),
    ('seed-xxe', 'xxe', 'vulnerability', 'XML 外部实体注入', 'manual', 0),
    ('seed-bac', 'broken_access_control', 'vulnerability', '访问控制失效', 'manual', 0),
    ('seed-misconfig', 'security_misconfiguration', 'vulnerability', '安全配置错误', 'manual', 0),
    ('seed-deser', 'deserialization', 'vulnerability', '不安全反序列化', 'manual', 0),
    ('seed-bof', 'buffer_overflow', 'vulnerability', '缓冲区溢出', 'manual', 0),
    ('seed-uaf', 'use_after_free', 'vulnerability', '释放后使用', 'manual', 0),
    ('seed-race', 'race_condition', 'vulnerability', '竞态条件', 'manual', 0);

-- Seed Relations: tool → technique
INSERT OR IGNORE INTO ontology_relations (relation_id, from_concept_id, to_concept_id, relation_type, confidence, source) VALUES
    ('seed-rel-nmap-scan', 'seed-nmap', 'seed-port-scan', 'implements', 1.0, 'manual'),
    ('seed-rel-nuclei-scan', 'seed-nuclei', 'seed-vuln-scan', 'implements', 1.0, 'manual'),
    ('seed-rel-sqlmap-sqli', 'seed-sqlmap', 'seed-sqli', 'implements', 1.0, 'manual');
