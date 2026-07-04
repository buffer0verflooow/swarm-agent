# Swarm Knowledge Base — 蜂群智能体知识库架构

> 基于 reverse-engine 项目的 DIKW 知识架构，为蜂群式多智能体系统设计的可进化知识库。
> 核心目标：支撑 Agent 间知识共享、本体演化、跨任务经验复用。

## 一、核心理念

```
                    蜂群智能体的知识循环
                   
    ┌─────────────────────────────────────────────────┐
    │                                                 │
    │   Agent₁ ──┐                                   │
    │   Agent₂ ──┼──→ 任务执行 ──→ 经验提取          │
    │   Agent₃ ──┘         │                          │
    │                      ▼                          │
    │              ┌──────────────┐                   │
    │              │   知识库      │                   │
    │              │  (DIKW 金字塔) │                  │
    │              └──────┬───────┘                   │
    │                     │                           │
    │   ┌─────────────────┼─────────────────────┐     │
    │   │  治理引擎        │   本体引擎           │     │
    │   │  • 反例衰减      │   • 关系推理         │     │
    │   │  • 聚类去重      │   • 概念泛化         │     │
    │   │  • 置信度传播    │   • 跨域映射         │     │
    │   └────────┬────────┴──────────┬──────────┘     │
    │            │                   │                │
    │            ▼                   ▼                │
    │    ┌──────────────┐    ┌──────────────┐        │
    │    │  净化后的知识  │    │  扩展后的本体  │        │
    │    │  (可检索复用)  │    │  (概念网络)    │        │
    │    └──────────────┘    └──────────────┘        │
    │                                                 │
    │   → 下次任务时其他 Agent 自动获取最新知识        │
    └───────────────────────────────────────────────��─┘
```

## 二、DIKW 金字塔（适配蜂群场景）

| 层级 | 名称 | 含义 (蜂群版) | 示例 |
|------|------|--------------|------|
| **D** | Data | Agent 执行任务的原始记录 | "Agent-7 用 nmap 扫了 10.0.0.1，发现 22/80/443 开放" |
| **I** | Information | 从记录中提取的结构化信息 | `{tool: nmap, target: 10.0.0.1, ports: [22,80,443], duration: 12s}` |
| **K** | Knowledge | 跨多次任务验证的规律/模式 | "内网主机普遍开放 22/445/3389，建议优先扫这三个端口" |
| **W** | Wisdom | 可指导 Agent 行为的元规则 | "扫描策略：先 ICMP 存活探测 → 再 TOP1000 端口 → 再全端口" |

## 三、本体模型（核心创新）

用两类层构建本体：

### 概念层 (Concept Layer)
```
实体类型:
├── Tool        (工具: nmap, nuclei, sqlmap, ...)
├── Technique   (技术: port_scan, sql_injection, priv_esc, ...)
├── Target      (目标: ip, domain, binary, apk, ...)
├── Vulnerability (漏洞: CVE-XXXX, OWASP Top10, ...)
├── Agent       (智能体角色: scanner, analyst, exploiter, ...)
└── Task        (任务: recon, exploit, report, ...)
```

### 关系层 (Relation Layer)
```
关系类型:
├── uses          (Agent 使用 Tool)
├── discovers     (Task 发现 Vulnerability)
├── depends_on    (Technique 依赖 Technique)
├── mitigates     (Technique 缓解 Vulnerability)
├── specializes   (子概念 → 父概念)
├── conflicts_with (互斥关系)
└── evolves_to    (知识随时间演化到新版本)
```

## 四、Schema 总览

```
┌──────────────────────────────────────────────────────────┐
│                     SQLite 单文件                         │
│                                                          │
│  ┌───────────┐  ┌──────────────┐  ┌───────────────────┐ │
│  │ swarm_runs│  │knowledge_    │  │ ontology_          │ │
│  │ agent_tasks│  │  entries     │  │  concepts          │ │
│  │ agent_     │  │  promotions  │  │  relations         │ │
│  │  delegations│ │  lineage    │  │  instances         │ │
│  └───────────┘  │  embeddings  │  └───────────────────┘ │
│                  │  clusters    │                        │
│                  │  counter_ex  │  ┌───────────────────┐ │
│                  │  wisdom      │  │ swarm_             │ │
│                  │  distilled_  │  │  strategies        │ │
│                  │    rules     │  │  agent_profiles    │ │
│                  └──────────────┘  │  swarm_behaviors   │ │
│                                    └───────────────────┘ │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────┐
│              ontology_* tables / in-process graph         │
│                                                          │
│  (:Concept)   -[:SPECIALIZES]-> (:Concept)               │
│  (:Agent)     -[:USES]->        (:Tool)                  │
│  (:Task)      -[:DISCOVERS]->   (:Vulnerability)         │
│  (:Knowledge) -[:DERIVED_FROM]-> (:Task)                 │
│  (:Knowledge) -[:RELATED_TO]->  (:Knowledge)             │
│  (:Knowledge) -[:IMPLEMENTS]->  (:Technique)             │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

## 五、与 reverse-engine 的差异

| 维度 | reverse-engine | Swarm Knowledge |
|------|---------------|-----------------|
| 领域 | 二进制逆向工程 | 通用多 Agent 知识 |
| 知识来源 | IDA Pro 分析结果 | 任意 Agent 任务执行 |
| 本体 | 隐式（函数/行为标签） | **显式本体层** (概念+关系) |
| 参与者 | 单一分析流水线 | **多个并发 Agent** |
| 演化 | 反例衰减 | 反例衰减 + **版本演化** + **策略蒸馏** |
| 存储 | PG + Memgraph | SQLite 单文件 + ontology 表 |
