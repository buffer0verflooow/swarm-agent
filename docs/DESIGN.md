# Swarm Knowledge Base — 设计文档

> 为蜂群式多智能体系统设计的可进化知识库
> 继承自 `reverse-engine` 项目的 DIKW 架构，新增显式本体层和蜂群策略库

## 快速开始

```bash
# 1. 初始化 PostgreSQL
createdb swarm_knowledge
psql swarm_knowledge -c "CREATE EXTENSION IF NOT EXISTS vector"
psql swarm_knowledge -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"  # 用于相似度

# 2. 运行迁移
for f in migrations/*.sql; do
    psql swarm_knowledge -f "$f"
done

# 3. 安装依赖
pip install -r requirements.txt

# 4. 运行治理周期
python -c "
from swarm_knowledge import run_promotion_cycle, run_full_clustering, run_ontology_maintenance
import asyncio
asyncio.run(run_promotion_cycle(pg_client))
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

```sql
-- 语义检索: 找相似的经验
SELECT title, content, trust_vector, level
FROM knowledge_entries
WHERE status = 'active'
  AND domain = 'network'
  AND level >= 2
ORDER BY embedding <-> (SELECT embedding FROM knowledge_entries WHERE id = 'query-id')
LIMIT 10;

-- 策略查询: 找最佳策略
SELECT rule_name, rule_description, priority
FROM distilled_rules
WHERE is_active = TRUE
  AND 'scanner' = ANY(applicable_agents)
  AND trigger_condition->>'intent' = 'recon'
ORDER BY priority DESC;

-- 本体推理: 找工具链
SELECT oc2.concept_name AS recommended_tool
FROM ontology_relations r
JOIN ontology_concepts oc1 ON r.from_concept_id = oc1.concept_id
JOIN ontology_concepts oc2 ON r.to_concept_id = oc2.concept_id
WHERE oc1.concept_name = 'port_scan'
  AND r.relation_type = 'implements';
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
├── migrations/
│   ├── 001_swarm_core.sql          # 蜂群核心表
│   ├── 002_knowledge_core.sql      # DIKW 知识金字塔
│   ├── 003_ontology.sql            # 本体模型
│   ├── 004_embeddings_strategies.sql # 向量 + 聚类 + 策略
│   └── 005_seed_ontology.sql       # 种子本体数据
├── src/
│   ├── __init__.py
│   ├── governance/
│   │   ├── __init__.py
│   │   ├── engine.py               # DIKW 提升 + 衰减 + 交叉验证
│   │   └── clustering.py           # 三层链接 + Louvain
│   ├── ontology/
│   │   ├── __init__.py
│   │   └── inference.py            # 概念发现 + 关系推理 + 漂移检测
│   ├── orchestrator/               # (预留) 蜂群编排逻辑
│   └── agents/                     # (预留) Agent 知识提取管道
├── docs/
│   ├── ARCHITECTURE.md             # 架构总览
│   └── DESIGN.md                   # 本文档
└── requirements.txt
```
