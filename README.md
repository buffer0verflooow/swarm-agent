# Swarm Knowledge Base — 通用自治蜂群系统

> 从"逆向专用"升级为**领域无关的通用自治多智能体系统**：任务拆解、动态领取、
> Agent 协同、状态共享、失败重规划与结果汇聚。SQLite 单文件存储，无外部依赖，
> 定时器驱动，可恢复、可调试。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 初始化数据库
python scripts/init_db.py

# 3. 运行测试 (85 项)
.venv/bin/python -m pytest tests/ -q

# 4. 提交一个蜂群任务 (CLI)
python scripts/swarmctl.py submit --goal "分析目标 X 的攻击面"

# 5. 启动蜂群运行
python scripts/start_swarm.py --run-id <run_id>
```

## 蜂群算法总览

```
任务提交 (swarmctl / client_api)
    │
    ▼
SwarmRunner ──→ create_swarm_run ──→ seed_swarm_run (初始任务入市场)
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  SwarmOrchestrator (定时器驱动主循环, SQLite 轮询状态)      │
│                                                         │
│  ┌─────────┐   ┌──────────────┐   ┌──────────────────┐  │
│  │ spawner │   │  work_queue  │   │    lifecycle     │  │
│  │ 5s 轮询 │   │  2s 轮询     │   │  10s 心跳清理     │  │
│  │ Agent 生成│  │ 任务领取/完成 │   │  僵尸 Agent 回收  │  │
│  └─────────┘   └──────┬───────┘   └──────────────────┘  │
│       │                │                                 │
│       ▼                ▼                                 │
│  ┌──────────────────────────────┐                        │
│  │  SwarmWorker (worker loop)   │                        │
│  │  注册 → 领取 → KB上下文注入 →  │                        │
│  │  executor 执行 → capture 沉淀  │                        │
│  └──────────────┬───────────────┘                        │
│                 │                                        │
│  ┌──────────────┼────────────────────────────────────┐   │
│  │ 治理/信号/控制层 (旁路, 定期运行)                    │   │
│  │  • governance 60s: DIKW 提升/衰减/聚类             │   │
│  │  • signals: worker 信号/循环检测/卡死检测/新颖度     │   │
│  │  • controller 60s: LLM 判决 (kill/boost/spawn/…)  │   │
│  │  • power schedule 15s: 预算→广度/深度切换           │   │
│  │  • action value: 行动价值重打分                     │   │
│  └────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
    │
    ▼
结果汇聚 → reports/ + KB 入库 (capture) + signal_board 共享
```

**完整算法细节见 [`docs/SWARM-ALGORITHM.md`](docs/SWARM-ALGORITHM.md)**。

## 核心设计

| 机制 | 位置 | 说明 |
|---|---|---|
| 任务图 (DAG) | `src/swarm/task_graph.py` | 高层目标 → 子任务 DAG，依赖门控发布、收据验证证据、运行中生长 |
| 共享信号板 | `src/swarm/signal_board.py` | 图级作用域 append-only 信号，worker 自动注入，失败静默降级 |
| 工作市场 | `src/swarm/work_queue.py` | 发布/领取/完成/失败 + 过期恢复（stale claim） |
| Agent 生成 | `src/swarm/spawner.py` | 去重/合并重复请求/过期清理/可恢复 |
| 生命周期 | `src/swarm/lifecycle.py` | 心跳注册 + 僵尸清理 |
| Worker 循环 | `src/swarm/worker.py` | 注册 → 领取 → KB 上下文 → executor → capture |
| Controller | `src/swarm/controller.py` | LLM 判决（kill/boost/spawn/redirect），rules 降级 |
| 探索痕迹 | `src/swarm/exploration.py` | 已探索路径记录，避免重复死路 |
| Worker 信号 | `src/swarm/signals.py` | 循环检测、卡死检测、新颖度评分 |
| 行动价值 | `src/swarm/action_value.py` | 按行动 ROI 重打分待办 |
| 模型配置 | `src/swarm/model_config.py` | 模型画像、任务级分配、事件记录 |
| 治理引擎 | `src/governance/` | DIKW 提升/衰减、交叉验证、聚类、wisdom、bounty 门控 |
| 知识捕获 | `src/agents/capture.py` | 任务结果 → KB，lineage 溯源 |
| 知识检索 | `src/agents/retrieval.py` | 搜索/相似/上下文注入/策略选择 |
| 本体引擎 | `src/ontology/` | 概念发现、关系推理、合并建议、漂移检测 |

## 目录结构

```
swarm-knowledge/
├── src/                  # 核心库 (Python)
│   ├── db.py             # SQLite 层 (SwarmDB)
│   ├── swarm/            # 蜂群运行时 (编排/市场/信号/控制)
│   ├── agents/           # 知识捕获与检索
│   ├── governance/       # 治理引擎 (DIKW/聚类/验证/bounty)
│   └── ontology/         # 本体引擎
├── scripts/              # CLI 入口 (swarmctl/capture/init_db/...)
├── benchmarks/           # 公开基准测试 (MARBLE/BountyBench)
├── docs/                 # 架构与设计文档
├── reports/              # 评估报告 (基准结果)
├── migrations/           # SQL 迁移 (001-015)
├── tests/                # 85 项测试
└── swarm_knowledge.db    # SQLite 数据库 (运行时生成)
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/SWARM-ALGORITHM.md](docs/SWARM-ALGORITHM.md) | **蜂群算法整体梳理**（本仓库核心） |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 知识库架构（DIKW/本体/治理） |
| [docs/DESIGN.md](docs/DESIGN.md) | 设计文档与快速开始 |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | 集成指南 |
| [docs/controller-worker-architecture.md](docs/controller-worker-architecture.md) | Controller/Worker 分层（Phase B） |
| [docs/signal-board.md](docs/signal-board.md) | 共享信号板设计 |
| [docs/task-graph-layer.md](docs/task-graph-layer.md) | 任务图层设计 |
| [docs/queen-bee-evaluation-codex.md](docs/queen-bee-evaluation-codex.md) | 蜂后评估 |
| [docs/role-gap-analysis-queen-prophet-guide.md](docs/role-gap-analysis-queen-prophet-guide.md) | 角色缺口分析（蜂后/先知） |
| [docs/HANDOFF-action-value-scheduling.md](docs/HANDOFF-action-value-scheduling.md) | 行动价值调度交接 |

## 基准测试结果（公开数据集）

| 基准 | 结果 | 报告 |
|---|---|---|
| MARBLE (database 100 任务) | 蜂群 100/100 (F1=1.000) vs 单 agent 70/100 (F1=0.873) | `reports/marble-full-baseline-2026-08-07.md` |
| BountyBench Detect (lunary 3 bounty) | 蜂群 2/3 = 单 agent 2/3（互补：并集 3/3） | `reports/bountybench-detect-pilot-2026-08-08.md` |
| BountyBench Exploit (lunary 3 bounty) | 蜂群 2/3 = 单 agent 2/3（互补模式跨档位稳定） | `reports/bountybench-exploit-pilot-2026-08-09.md` |
| BountyBench Patch (lunary 3 bounty) | **蜂群 3/3 全首轮 = 单 agent 3/3**（> 基线 87.5%） | `reports/bountybench-patch-pilot-2026-08-09.md` |
| BountyBench 扩样本 (gradio 3 bounty) | 单 agent 1/3 vs 蜂群 2/3（波动） | `reports/bountybench-expand-sample-2026-08-09.md` |

**关键结论**：蜂群（多 verifier 广覆盖）与单 agent（自由探索非常规漏洞）呈稳定互补，
合并覆盖 = 双档位 6/6；Patch 档蜂群三角色分工实现 0 迭代首轮命中。
三档均达到/超过官方基线（Detect 5% / Exploit 57.5% / Patch 87.5%）。
完整总结见 `reports/bountybench-tiers-summary-2026-08-09.md`。

## 版本

v0.3.0 — 见 `src/__init__.py`
