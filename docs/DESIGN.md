# Swarm Knowledge Base — 设计文档

> 为蜂群式多智能体系统设计的可进化知识库
> 继承自 `reverse-engine` 项目的 DIKW 架构，新增显式本体层和蜂群策略库

## 快速开始

```bash
# 1. 安装可选开发依赖
pip install -r requirements.txt

# 2. 初始化 SQLite 单文件数据库
python init_db.py

# 3. 查看统计
python init_db.py --stats

# 4. 运行集成测试
python tests/test_swarm_loop.py

# 5. 手动运行治理周期
python -c "
from src import SwarmDB, run_promotion_cycle
db = SwarmDB('swarm_knowledge.db')
print(run_promotion_cycle(db))
"
```

## 数据流向

```
Agent 执行任务
    │
    ├─→ swarm_runs (创建运行记录)
    ├─→ agent_tasks (创建任务记录)
    ├─→ agent_delegations (委派审计)
    │
    ▼
知识提取 (Agent 或自动)
    │
    ├─→ knowledge_entries (D/I 级)
    ├─→ knowledge_lineage (溯源)
    │
    ▼
治理引擎 (周期运行)
    │
    ├─→ DIKW 提升 (D→I→K→W)
    ├─→ 交叉验证 (trust_vector 更新)
    ├─→ 反例衰减 (counter_examples → stale)
    │
    ▼
聚类引擎 (周期运行)
    │
    ├─→ Weak Links (embedding 相似度)
    ├─→ Strong Links (共享 lineage)
    ├─→ Communities (Louvain)
    │
    ▼
本体引擎 (周期运行)
    │
    ├─→ 概念发现 (从任务中提取)
    ├─→ 关系推理 (传递闭包)
    ├─→ 概念合并建议
    ├─→ 概念漂移检测
    │
    ▼
策略库 (Agent 查询)
    │
    └─→ 为后续任务提供最优策略
```

## 漏洞赏金知识循环

DIKW 回答“这条知识处于什么可信层级”，漏洞赏金还需要回答“这条候选发现是否值得提交”。系统把这部分做成覆盖在 `knowledge_entries` 上的工作流层：

```
knowledge_entries(type=vulnerability)
    │
    ▼
finding_hypotheses
    │
    ├─→ ROI 排序: expected_payout / estimated_hours * competition_factor
    │
    ├─→ validation gates:
    │     poc_exists → clean_repro → impactful → low_priv_reachable → in_scope → deduplicated
    │
    ├─ 全部通过 → validation_status=validated, knowledge_entries.level 至少提升到 L3
    │
    └─ 任一失败 → negative_knowledge + counter_examples，供下次 campaign 避免重复死路
```

这不是替代 DIKW，而是把文章里的 hallucination bin/Gate 0-3 思路接进当前知识库。新发现默认是 `hypothesis`，只有经过门控证据才是 `validated`。低权限不可达、不可复现、无安全影响、出 scope、重复报告等失败结论会沉淀为 `negative_knowledge`。

## 核心 API

### 知识写入 (Agent 侧)

```python
# Agent 完成分析后写入知识
INSERT INTO knowledge_entries (
    level, knowledge_type, content, title,
    source_agent, source_run_id, domain, knowledge_intent
) VALUES (
    1, 'observation',
    '端口扫描发现目标 192.168.1.1 开放了 22(SSH), 80(HTTP), 443(HTTPS), 3306(MySQL)',
    '扫描发现: 192.168.1.1 多端口开放',
    'scanner-3', 'run-uuid-here',
    'network', 'enumerate'
);

-- 写入 lineage
INSERT INTO knowledge_lineage (
    knowledge_id, source_type, source_ref, extraction_method
) VALUES (
    'entry-uuid', 'agent_execution',
    '{"agent_id": "scanner-3", "tool_name": "nmap", "run_id": "run-uuid"}',
    'agent_analysis'
);
```

### 知识查询 (Agent 侧)

```python
from src import SwarmDB, search, get_active_rules

db = SwarmDB("swarm_knowledge.db")

# 全文检索: 找相关经验
results = search(db, "如何绕过 ASLR", domain="security", level_min=2)

# 策略查询: 找适合 scanner 的规则
rules = get_active_rules(db, agent_role="scanner", intent="recon")

# 本体查询: 找 port_scan 的实现工具
tools = db.fetch_all("""
    SELECT oc1.concept_name AS tool
    FROM ontology_relations r
    JOIN ontology_concepts oc1 ON r.from_concept_id = oc1.concept_id
    JOIN ontology_concepts oc2 ON r.to_concept_id = oc2.concept_id
    WHERE oc2.concept_name = 'port_scan'
      AND r.relation_type = 'implements'
""")
```

### 赏金假设门控

```python
from src import (
    create_finding_hypothesis,
    record_gate_result,
    rank_hypotheses_by_roi,
    get_negative_knowledge,
)

# 1. 把漏洞知识条目转为“候选报告假设”
hypothesis = create_finding_hypothesis(
    db,
    knowledge_id="entry-uuid",
    target_id="vendor-agent",
    program="vendor-bounty",
    vulnerability_class="lpe",
    expected_payout=5000,
    estimated_hours=20,
    competition_factor=0.8,
)

# 2. 逐个记录门控证据
record_gate_result(db, hypothesis["hypothesis_id"], "poc_exists", "pass", evidence="PoC runs")
record_gate_result(db, hypothesis["hypothesis_id"], "clean_repro", "pass", evidence="clean VM snapshot")
record_gate_result(db, hypothesis["hypothesis_id"], "impactful", "pass", evidence="standard user to SYSTEM")
record_gate_result(db, hypothesis["hypothesis_id"], "low_priv_reachable", "pass", evidence="lowpriv user confirmed")
record_gate_result(db, hypothesis["hypothesis_id"], "in_scope", "pass", evidence="program scope page")
record_gate_result(db, hypothesis["hypothesis_id"], "deduplicated", "pass", evidence="no duplicate in KB")

# 3. 下一轮 campaign 前先看 ROI 和负结果
ranked = rank_hypotheses_by_roi(db)
dead_ends = get_negative_knowledge(db, target_id="vendor-agent")
```

## 与 reverse-engine 的对应关系

| reverse-engine 表 | Swarm Knowledge 表 | 差异 |
|---|---|---|
| `analysis_runs` | `swarm_runs` | 支持多 Agent 并发 |
| `analysis_tasks` | `agent_tasks` | 新增 parent_task_id |
| `agent_delegations` | `agent_delegations` | 基本相同，新增跨 Agent |
| `knowledge_entries` | `knowledge_entries` | 新增 domain, subdomain, superseded_by |
| `knowledge_lineage` | `knowledge_lineage` | 新增 swarm_emergence, cross_agent_validation |
| `distilled_rules` | `distilled_rules` | 新增 trigger_condition, applicable_agents |
| `counter_examples` | `counter_examples` | 基本相同 |
| *(不存在)* | `agent_profiles` | 新增 |
| *(不存在)* | `swarm_behaviors` | 新增：记录涌现行为 |
| *(不存在)* | `swarm_strategies` | 新增：策略库 |
| *(不存在)* | `ontology_concepts` | 新增：显式本体 |
| *(不存在)* | `ontology_relations` | 新增：关系网络 |
| *(不存在)* | `ontology_instances` | 新增：运行时实例 |
| *(不存在)* | `finding_hypotheses` | 新增：漏洞赏金候选发现门控 |
| *(不存在)* | `finding_validation_gates` | 新增：PoC/复现/影响/权限/scope/去重证据 |
| *(不存在)* | `negative_knowledge` | 新增：不可提交、不可达、不可复现等负结果 |

## 演化路线图

```
Phase 1 (当前): 核心 Schema + 治理引擎 + 种子本体
    ↓
Phase 2: Agent 知识提取管道
    - 每个 Agent 任务完成后自动提取 knowledge_entries
    - 从 swarm_behaviors 中检测涌现模式
    ↓
Phase 3: 自适应策略
    - 基于 swarm_strategies 的成功率自动调整策略权重
    - 概念漂移触发自动概念演化
    ↓
Phase 4: 跨蜂群知识共享
    - 多个蜂群的知识库联邦查询
    - 本体对齐 (ontology alignment)
```

## 文件结构

```
swarm-knowledge/
├── agent_worker.py                  # Agent 侧任务市场 CLI
├── start_swarm.py                   # 创建 run 并发布市场 seed tasks
├── swarmctl.py                      # 模型 profile、对话事件、run summary 控制面
├── migrations/
│   ├── 001_schema.sql              # SQLite 核心表 + 种子本体
│   ├── 002_swarm_extensions.sql    # spawn_requests + heartbeats
│   ├── 003_architecture_fixes.sql  # token/pheromone/power schedule
│   ├── 004_verification_wisdom.sql # 验证队列 + wisdom 字段
│   ├── 005_spawn_claims.sql        # spawn claim 恢复时间戳
│   ├── 006_work_market.sql         # agent_tasks 共享任务市场
│   ├── 007_model_profiles.sql      # 蜂群自维护模型 profile + 对话事件
│   ├── 008_raw_events_artifacts.sql # 原始事件 + artifact 父进程校验
│   └── 009_bounty_knowledge_loop.sql # 赏金假设门控 + 负结果
├── src/
│   ├── __init__.py
│   ├── governance/
│   │   ├── __init__.py
│   │   ├── engine.py               # DIKW 提升 + 衰减 + 交叉验证
│   │   ├── bounty.py               # 赏金假设门控 + ROI + negative knowledge
│   │   └── clustering.py           # 三层链接 + Louvain
│   ├── ontology/
│   │   ├── __init__.py
│   │   └── inference.py            # 概念发现 + 关系推理 + 漂移检测
│   ├── swarm/                      # client API、编排、生命周期、spawn 信号、任务市场、模型配置、worker loop、run manager
│   └── agents/                     # capture/retrieval/extractor
├── docs/
│   ├── ARCHITECTURE.md             # 架构总览
│   └── DESIGN.md                   # 本文档
└── requirements.txt
```
