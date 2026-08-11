# 蜂群算法整体梳理（SWARM-ALGORITHM）

> 本文梳理 `src/swarm/` 的完整运行机制：从任务提交到结果返回的端到端流程、
> 每个模块的算法细节、关键参数与设计权衡。配合 `README.md` 使用。

## 0. 设计哲学

- **SQLite 即状态**：所有运行状态（任务、Agent、信号、决策）持久化在
  `swarm_knowledge.db`，无内存态。崩溃恢复 = 重读数据库。
- **定时器驱动**：Orchestrator 按固定节拍轮询，无事件总线。简单、可调试。
- **市场机制**：任务发布到共享市场，Worker 按角色领取——解耦生产者/消费者，
  天然支持动态扩缩。
- **领域无关**：role / task_type / tool_allowlist 都是普通字符串。
  逆向/Web/取证只是"插件"（角色+工具的集合），核心不感知领域。

## 1. 端到端数据流

```
① 提交:   swarmctl.py submit / client_api.submit_swarm_task
② 建运:   SwarmRunner → create_swarm_run → seed_swarm_run
           (初始任务按角色计数发布进 agent_tasks 市场)
③ 编排:   SwarmOrchestrator.run_loop 常驻 (见 §2 节拍)
④ 领取:   SwarmWorker 注册 Agent → claim_work_tasks (角色匹配 + 市场领取)
⑤ 执行:   build_task_context (KB 上下文注入) → executor(外部进程/LLM)
⑥ 沉淀:   capture() 知识入库 + record_worker_signal + 任务完成/失败
⑦ 汇聚:   reporter/lead 角色汇总 → reports/ + run summary
⑧ 治理:   周期运行 (见 §7) 提升知识质量、淘汰死路
```

## 2. Orchestrator 主循环（定时器节拍）

`src/swarm/orchestrator.py` — `SwarmOrchestrator.run_loop`

| 节拍 | 间隔 | 动作 |
|---|---|---|
| POLL_WORK_SEC | 2s | 轮询工作市场：发布/领取/过期恢复 |
| POLL_SPAWN_SEC | 5s | 轮询 spawn_requests → 生成新 Agent |
| POLL_HEARTBEAT_SEC | 10s | 心跳清理：回收僵尸 Agent |
| POLL_POWER_SCHEDULE_SEC | 15s | 预算驱动的广度/深度切换（§6.3） |
| POLL_HEALTH_SEC | 30s | 健康检查 |
| POLL_GOVERNANCE_SEC | 60s | 治理循环：DIKW 提升/衰减/聚类（§7） |
| POLL_CONTROLLER_SEC | 60s | Controller LLM 判决：kill/boost/spawn/redirect（§6.2） |

关键约束：`MAX_AGENTS_PER_RUN = 8` — 单次 run 最多 8 个并发活跃 Agent。

## 3. 任务图（Task Graph）— 高层目标分解

`src/swarm/task_graph.py` — 领域无关的目标分解层，叠加在工作市场上。

### 3.1 图结构

- `create_task_graph(run_id, goal, strategy)` → graph_id
- `add_task_node(task_key, goal, role, task_type, depends_on, acceptance_criteria, tool_allowlist, budget)`
  → DAG 节点，含显式依赖、验收判据、工具白名单、预算
- 一个 run 可挂多个图；节点发布进 `agent_tasks` 市场，Worker 领取方式不变

### 3.2 发布门控（依赖）

- `publish_ready_nodes`：只发布根节点（无未完成依赖）
- 子节点在依赖节点全部完成后才可领取——天然拓扑排序
- `spawn_subtask`：**运行中生长**（rolling admission）——完成的节点可
  发射新的子任务规格入图，替代一次性静态 DAG（借鉴 reverselibrary）

### 3.3 验收与证据

- `evaluate_acceptance(task_id, result)`：对照 acceptance_criteria
  （metric + op + value + required）判定节点成败
- `record_task_evidence` + `verify_evidence_receipts`：**收据验证**——
  证据的 `ref` 必须逐字出现在已完成工具调用的请求/响应中才算数，
  裸指标永远不能单独通过验收（防伪造）
- `complete_graph_task` / `mark_graph_completed`：节点完成 → 图完成

### 3.4 图进度

- `get_graph_progress(run_id)`：汇总各节点状态，供 lead/reporter 汇聚

## 4. 共享信号板（Signal Board）— 图级状态共享

`src/swarm/signal_board.py` — 蜂群核心模块（MARBLE 实证固化）。

| API | 作用 |
|---|---|
| `publish_signal(graph_id, key, value)` | 发布图级信号（append-only） |
| `get_signals / get_signal` | 读取信号 |
| `attach_evidence(graph_id, node_key, evidence)` | 给节点挂证据 |
| `collect_evidence(graph_id)` | 汇聚节点证据 |
| `get_graph_id_for_task(task_id)` | 任务 → 所属图 |
| `build_signal_context(task, max_chars)` | 构建 worker 上下文注入 |

设计要点：
- **图级作用域**：信号只在本图内可见（不是全局广播）
- **append-only**：信号只增不改，历史可追溯
- **自动注入**：worker 领取任务时自动带 `build_signal_context`
- **失败静默降级**：无图时信号板调用直接返回空，不阻塞主流程

实证：MARBLE task 97 容错（verifier SSL 超时 → lead 从信号板独立补报）。

## 5. Worker 循环与工作市场

### 5.1 市场 (`work_queue.py`)

```
publish_work_task → agent_tasks (待领取)
poll_work_tasks   → 可见任务列表
claim_work_tasks  → 原子领取 (状态 pending→claimed, 记 claimant)
complete_work_task / fail_work_task → 终态
recover_stale_work_claims → 过期认领回收 (crash 恢复)
```

### 5.2 Worker (`worker.py`)

`SwarmWorker.run_one` 流程：
1. 注册 Agent（lifecycle：心跳）
2. `claim_work_tasks` — 按 role 匹配领取
3. `build_task_context` — KB 检索注入（相似知识/策略/信号板）
4. `resolve_task_model_profile` — 按任务解析模型画像
5. 调 executor（外部命令/LLM worker），`normalize_executor_result`
6. `capture()` 沉淀知识 + `record_worker_signal` 上报信号
7. `complete/fail_work_task` + 验收（若在图内）+ `verify_artifacts`

### 5.3 生成 (`spawner.py`)

```
request_spawn → pending
claim_spawn_requests → orchestrator 领取
mark_spawn_fulfilled / mark_spawn_rejected
build_spawn_dedup_key (run_id+role+reason) → 去重
merge_duplicate_requests / expire_old_requests / recover_stale_spawn_claims
```

## 6. 智能控制层

### 6.1 Worker 信号 (`signals.py`)

- `record_worker_signal`：worker 每步上报（类型/内容/元数据）
- `detect_loops`：循环检测（重复信号模式）
- `get_stuck_workers`：卡死检测（长时间无进展）
- `compute_novelty_score`：新颖度评分（与历史信号的差异度）

### 6.2 Controller（LLM 判决）(`controller.py`)

每 60s 审视全部 worker 信号，做 **kill / boost / spawn / redirect** 决策。

- LLM 模式：调 Opus 级模型（config.yaml custom_providers），温度 0.3，
  每次 tick 最多 3 个决策
- Rules 降级：LLM 不可用时纯规则（如质量 <0.25 或卡死 3 分钟 → kill）
- 决策持久化到 controller_decisions 表，可审计

### 6.3 Power Schedule（预算调度）(`orchestrator.py` 常量)

```
BUDGET_BREADTH_THRESHOLD = 0.3  # 预算 <30% 用尽 → breadth (广撒网)
BUDGET_DEPTH_THRESHOLD   = 0.7  # 预算 >70% 用尽 → depth (集中深挖)
MAX_CHAIN_DEPTH_DEFAULT  = 3    # 追链最大深度
VULN_DENSITY_THRESHOLD   = 0.15 # 知识中漏洞占比 >15% → 切 depth
```

预算充裕时广撒网（多方向并行），预算吃紧或发现密集时集中深挖。

### 6.4 探索痕迹 (`exploration.py`)

- `record_trace`：记录已探索的 URL/路径（literal，无语义归一化）
- `get_explored_for_target`：查某目标已探索内容
- `get_exhausted_paths(threshold)`：判定死路（重复失败超过阈值）
- `build_exploration_context`：注入 worker，避免重复死路
- `get_unexplored_hints`：给"未探索端点"提示

### 6.5 行动价值 (`action_value.py`)

`maybe_rescore_pending`：按行动 ROI（预期收益/成本）重打分待办任务，
影响领取顺序。

## 7. 治理循环（每 60s）

`src/governance/` — 知识质量保障：

| 引擎 | 作用 |
|---|---|
| 提升 (promotion) | D→I→K→W 层级提升（重复验证的知识升级） |
| 衰减 (decay) | 反例/超时 → stale（防止过时知识误导） |
| 交叉验证 | 多源一致 → trust_vector 提升 |
| 聚类 (Louvain/TF-IDF) | 去重、发现社区 |
| 本体引擎 | 概念发现、关系推理、合并建议、漂移检测 |
| Bounty 门控 | finding_hypotheses 的 ROI 排序 + validation gates |
| Wisdom 蒸馏 | K→W 元规则 |

## 8. 失败重规划（关键机制）

系统在多层面处理失败，层层递进：

```
① 任务层: fail_work_task + recover_stale_work_claims (crash 恢复)
② Agent 层: 心跳超时 → 僵尸清理 + spawner 重新生成
③ 执行层: worker 失败反馈 → executor 迭代修正 (benchmark 实证: 4 轮)
④ 方向层: controller 判决 kill 错误方向 / redirect
⑤ 盲区层: FREE-AUDIT (程序化审计) + free-explore (无假设自由探索)
           —— 假设清单分类粒度决定蜂群天花板, 自由探索补盲区
```

## 9. 关键设计权衡（来自评估实证）

| 权衡 | 选择 | 实证 |
|---|---|---|
| 状态存储 | SQLite 轮询 vs 事件总线 | 简单可恢复；5s 轮询延迟可接受 |
| 任务分发 | 市场领取 vs 直接指派 | 动态扩缩；角色匹配需 agent 自报 role |
| 知识共享 | 图级信号板 vs 全局广播 | 隔离性；图内上下文注入 |
| 验证方式 | 执行式 (P5) vs 报告式 | 防捏造；报告必须与产物一致 |
| 蜂群 vs 单 agent | 互补并存 (递进) | 蜂群广覆盖流程漏洞, 单 agent 深挖非常规 |

## 10. 运行模式

| 入口 | 用途 |
|---|---|
| `scripts/swarmctl.py submit` | 外部客户端提交任务 |
| `scripts/swarmctl.py run --target X` | 播种初始任务入市场 |
| `scripts/swarm_runner.py` | 本地多 worker 池运行 |
| `scripts/agent_worker.py --claim-only` | 单 worker 领取一步 |
| `scripts/capture.py` | 子 agent 一键知识捕获 |
| `scripts/init_db.py` | 初始化/统计/重建 DB |
