# 蜂群数据处理流程图（Swarm Data Flow）

> 基于 src/swarm/ 实际代码绘制（2026-08-10），每步标注真实模块/函数。
> 核心思想：**stigmergy（间接协作）**——Agent 不直接通信，通过共享数据
> 载体（任务市场 / 信号板 / KB / spawn 信号）协作。

## 一、总览（数据流骨架）

```
┌─────────────┐  ① 提交任务
│ client_api  │───────────────┐
└─────────────┘               ▼
                     ┌──────────────────┐
                     │   run_manager    │ ② 播种任务市场
                     │  (seeding)       │
                     └──────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
 ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
 │  task_graph  │  │ work_queue   │  │  spawn 机制  │
 │  DAG 分解    │─▶│  任务市场     │  │  stigmergy  │
 │  子任务入市场 │  │  agent_tasks │◀─┤  角色生成    │
 └──────────────┘  └──────┬───────┘  └──────────────┘
                          │ ③ claim (角色匹配)
                          ▼
                 ┌──────────────────┐
                 │    worker 循环   │ ④ 执行 + 验证 + 捕获
                 │  SwarmWorker     │
                 └────────┬─────────┘
                          │ ⑤ capture
                          ▼
                  ┌───────────────┐
                  │   knowledge   │ ⑥ 知识库 (KB)
                  │  entries + FTS│
                  └──────┬────────┘
                         │ ⑦ 治理循环 (60s)
                         ▼
                 ┌──────────────────┐
                 │  governance      │ ⑧ DIKW 提升/衰减/聚类/验证
                 │  engine          │
                 └────────┬─────────┘
                          │ ⑨ 新知识 → 新任务/spawn
                          ▼
                  ┌───────────────┐
                  │  信号 & 判决   │ ⑩ controller kill/boost/spawn
                  │  signals       │
                  └───────────────┘
```

## 二、Worker 循环详图（数据流的"心脏"）

```
┌─────────────────────────────────────────────────────────────┐
│ SwarmWorker.run_once()                                      │
│                                                             │
│  ① claim_once()                                             │
│     └─ claim_work_tasks(db, run_id, agent_id, role)         │
│        ├─ 角色匹配: required_role == self.role              │
│        ├─ 状态机: pending → claimed (原子 UPDATE ... WHERE) │
│        └─ 若空 → 心跳 beat(load=0) → 返回 None              │
│                                                             │
│  ② 模型配置                                                 │
│     └─ resolve_task_model_profile(db, task)                 │
│        └─ 任务可指定 model_profile（按角色/任务类型）        │
│                                                             │
│  ③ build_task_context(db, task)                             │
│     └─ focus_params.context_entry_ids → 取 KB 条目          │
│        └─ 图任务附加 signal_board 共享信号                   │
│                                                             │
│  ④ executor(task, context)  ← 真正的 LLM 调用               │
│     └─ normalize_executor_result(raw)                       │
│        ├─ success / content / artifacts / tags / metadata   │
│        └─ 失败 → fail_work_task + 心跳 → return              │
│                                                             │
│  ⑤ verify_artifacts(db, ...)   ← P5 铁律                   │
│     └─ 子 agent 自报的文件必须 stat/读回验证                 │
│        └─ 失败 → fail_work_task("artifact verification")    │
│                                                             │
│  ⑥ complete_work_task(db, task_id, ...)                     │
│     └─ 状态机: claimed → completed + 记录结果                │
│                                                             │
│  ⑦ _capture_task_result()  ← 知识入库                       │
│     └─ CaptureContext(source=TASK_RESULT, ...)               │
│        └─ capture(db, ctx, auto_classify=True)               │
│           └─ 写入 knowledge_entries + FTS 索引               │
│                                                             │
│  ⑧ record_worker_signal(db, ...)  ← 质量信号               │
│     └─ 信号 = agent_id + task_type + 时长 + 结果 + 内容 hash │
│        └─ compute_novelty_score / detect_loops 消费          │
└─────────────────────────────────────────────────────────────┘
```

## 三、任务数据流（work_queue / agent_tasks 表）

```
                    publish_work_task()
                    (来源: run_manager 播种 / task_graph / spawner / 治理)
                           │
                           ▼
                  ┌─────────────────┐
                  │   agent_tasks   │  ← 共享任务池 (SQLite)
                  │  status 状态机  │
                  └─────────────────┘
    pending ──claim──▶ claimed ──complete──▶ completed
        │                │   │                  │
        │                │   └──fail──────────▶ failed
        │                │
        │           recover_stale_work_claims()  ← 僵尸清理
        │           (claimed 超时 → 回滚 pending)
        ▼
   poll_work_tasks()   ← worker 按角色轮询
        │
        └─ 调度策略 (action_value.py)
           ├─ 默认: priority (静态优先级)
           └─ opt-in: action-value = success_probability
                       + exploration_bonus - avg_tokens
```

## 四、Stigmergy：spawn 信号流（蜂群核心机制）

```
┌──────────┐                    ┌─────────────────┐
│  Worker  │  ⑧ 产出知识条目      │  knowledge_entries│
└──────────┘──────────────────▶  └────────┬────────┘
                                           │ _auto_spawn_roles()
                                           │ vulnerability→analyst
                                           │ level≥3 → exploiter
                                           ▼
                                  ┌─────────────────┐
                                  │  spawn_requests │ ← 信号表
                                  │  (pending)      │
                                  └────────┬────────┘
                                           │ ⑩ orchestrator 轮询 (5s)
                                           │ _tick_spawn / _tick_stigmergy
                                           ▼
                                  ┌─────────────────┐
                                  │  spawn_handler  │
                                  │  HermesSpawn    │ ← delegate_task
                                  └────────┬────────┘
                                           │ 注入 KB 上下文
                                           ▼
                                  ┌─────────────────┐
                                  │  子 Agent 启动    │
                                  │  register →      │
                                  │  心跳 → 进入     │
                                  │  worker 循环      │
                                  └─────────────────┘
```

关键点：**Agent 不直接生成子 Agent**——只在 KB 留下"该生成什么角色"的信号，
Orchestrator 统一轮询实例化（异步解耦，防失控递归）。

## 五、治理循环（governance，60s tick）

```
        knowledge_entries (全量)
              │
              ▼
   ┌────────────────────────────┐
   │  governance/engine.py      │
   │  ① DIKW 提升: observation→│
   │     fact→mechanism→pattern │
   │      (按交叉验证证据计数)   │
   │  ② 反例衰减: counter_      │
   │     example → 降级/失效     │
   │  ③ 聚类去重: tfidf_cluster │
   │     → 合并重复条目          │
   │  ④ 验证: verification.py   │
   │     → 独立复核 (P5)        │
   │  ⑤ bounty.py: 发现奖励     │
   └────────────┬───────────────┘
                │ 提升后的知识
                ▼
        新知识 → 新任务 (publish_tasks_for_knowledge)
        新证据 → 任务图回写 (record_task_evidence)
```

## 六、Controller 判决流（Phase B，60s）

```
   signals 表 (worker 信号)
        │
        ▼
  _gather_worker_summary()  ← 每个 worker 的信号聚合
  _gather_global_state()    ← 全局状态 (市场/KB/任务图)
        │
        ▼
  Controller._tick() ── llm 模式 (Opus 级判决)
        │            └─ rules 模式 (规则判决, 降级备用)
        ▼
  _execute_decision()
   ├─ _execute_kill()      ← 低价值 worker 停止
   ├─ _execute_boost()     ← 高价值 worker 加预算
   ├─ _execute_spawn()     ← 缺口角色生成 (spawn_requests)
   └─ _execute_adjust_budget() ← 角色预算调整
```

## 七、任务图（task_graph，DAG）

```
   create_task_graph(run_id, goal)
        │
        ▼
   add_task_node(...)  ← 节点: {deps, required_role, metrics, evidence}
        │
        ▼
   publish_ready_nodes()  ← 依赖满足的节点 → 任务市场
        │                          ▲
        │                          │
   worker 执行 ◀────────────────────┘
        │
        ▼
   record_task_evidence()  ← 结果回写节点
        │
        ▼
   evaluate_acceptance()  ← 按 metrics 判定节点是否通过
        │
        ├─ 通过 → spawn_subtask() 下一层 / mark_graph_completed()
        └─ 失败 → 反馈回任务市场 (重试/换角色)
```

## 八、端到端数据流（一个完整例子）

```
用户: "分析 target.com 的认证漏洞"
  │
  ▼
client_api.submit_task()
  ▼
run_manager 播种: [discover 认证端点] [analyze 漏洞] [exploit 验证]
  ▼
task_graph 分解成 DAG: discover → analyze → exploit (依赖链)
  ▼
publish_ready_nodes() → 任务市场 (agent_tasks, pending)
  ▼
worker A (discoverer) claim → 执行 → 产出: 端点清单 + 知识条目
  ▼
capture() → KB (level=2 observation) + record_worker_signal()
  ▼
_auto_spawn_roles(): observation level≥3? 否 → 不入 spawn
  ▼
治理循环: 交叉验证 → observation → fact (level 提升)
  ▼
publish_tasks_for_knowledge() → 新任务: [分析端点 X] 入市场
  ▼
worker B (analyst) claim → 执行 → 产出: 漏洞假设 + 证据
  ▼
capture() → KB (level=3 mechanism) → spawn_requests: exploiter
  ▼
orchestrator 5s 轮询 → HermesSpawnHandler → delegate_task(exploiter)
  ▼
worker C (exploiter) claim → 构造 exploit → verify_artifacts → 崩溃验证 ✅
  ▼
complete_work_task + record_task_evidence (节点通过)
  ▼
task_graph: analyze 节点通过 → 下钻子任务 / mark_graph_completed
  ▼
治理: mechanism → pattern (DIKW 提升) + bounty 奖励
  ▼
最终: 用户拿到完整报告 (run 结果)
```

## 九、设计意图速查

| 机制 | 解决的问题 | 关键设计 |
|---|---|---|
| 任务市场 (work_queue) | Agent 间解耦 | SQLite 原子 claim，角色匹配 |
| Stigmergy (spawn_requests) | 防递归失控 | Agent 不直接 spawn，信号+统一实例化 |
| 信号 (signals) | 质量量化 | 每任务记录 → controller 判决 |
| 治理 (governance) | 知识保鲜 | DIKW 提升 + 反例衰减 + 聚类去重 |
| 任务图 (task_graph) | 目标导向 | DAG 依赖 + 证据回写 + 接受判定 |
| P5 验证 (artifacts) | 防捏造 | 自报文件必须 stat/读回验证 |
| Controller (Phase B) | 元控制 | LLM 判决 kill/boost/spawn |

## 十、任务上下文机制（analyst 如何知道要分析什么）

三层机制，全部通过数据传递（零直接通信）。

### 第 1 层：任务定义（claim 时拿到"为什么"）

`build_task_context()`（src/swarm/worker.py）组装 worker prompt，4 块拼成：

```
## Task
analyze for analyst                    ← 任务类型 + 角色
Intent: analyze                        ← run 级意图
Reason: 独立分析漏洞发现 [a1b2c3d4] 的影响、根因和边界条件   ← 为什么做这个
## Run Summary
分析 demo-app 登录接口的认证逻辑是否存在漏洞                 ← run 目标
## Context Entries                       ← 上游知识全文（最多 5 条）
[知识条目全文: 标题 + content + level + tags]
```

分析对象不在任务字段里，而在 `focus_params.context_entry_ids` 指向的 KB
条目——claim 时实时从 KB 拉取全文。

### 第 2 层：任务生成（谁决定分析什么 = 上游知识类型）

`publish_tasks_for_knowledge()`（src/swarm/work_queue.py）知识 → 任务 fan-out 规则表：

| 知识类型 (ktype, intent) | fan-out 任务 | 优先级 |
|---|---|---|
| (vulnerability, attack) | analyze + exploit + report | 80 / 90 / 65 |
| (technique, attack) | analyze + exploit | 70 / 75 |
| (pattern, enumerate) | scan + analyze | 70 / 55 |
| (strategy, defend) | report | 60 |
| level ≥ 3（任何类型） | report（高置信进报告） | 55 |

关键：analyst 分析什么不由人指定，由上游 worker 产出的知识类型自动决定。
发现 vulnerability → 自动有 analyst 独立分析影响/根因/边界；
发现 technique → 有人评估是否适用。

### 第 3 层：run 目标注入

`swarm_runs.conversation_summary` 实时汇总（每 tick 更新）注入所有 worker，
保证大目标不迷失。

**一句话**：分析对象 = context_entry_ids 指向的知识条目；为什么分析 = reason
（由上游知识类型规则生成）；大背景 = run summary。

## 十一、DIKW 晋升机制（规则 / 无审批设计 / 延迟分析）

### 等级体系

```
level 1  observation  原始观察（单个 worker 产出）
level 2  fact         事实（被独立印证）
level 3  mechanism    机制（根因/原理，多次印证）
level 4  pattern      模式（可复用规律）
```

### 晋升判定（PROMOTION_THRESHOLDS, src/governance/engine.py）

| 目标等级 | 印证来源数 | 复合置信度 |
|---|---|---|
| →2 (fact) | ≥ 1 | ≥ 0.60 |
| →3 (mechanism) | ≥ 2 | ≥ 0.75 |
| →4 (pattern) | ≥ 3 | ≥ 0.85 |

- **印证数** = knowledge_lineage 中 confidence_contribution > 0.5 的
  **不同 source_type 数**（防同一 agent 刷票）
- **复合置信度** = 0.4×base_confidence + 0.3×logic_soundness + 0.3×cross_validation
- cross_validation 由其他 agent 投票调整：`cross_validate()` 同意 +0.10 /
  反驳 -0.15（反驳记 counter_examples）

### 下行机制（对称）

- 反例衰减：counter_examples ≥ 5（COUNTER_THRESHOLD）→ 规则失效/知识降级
- 时间衰减：信息素 0.95^t 指数淡化

### 无审批设计（刻意）

**不存在审批机制——是"自动晋升"（promotion），不是"审批"（approval）**：

```python
# run_promotion_cycle() 每 60s 治理 tick:
扫描 status='active' 条目 (LIMIT 500)
for row: count=统计印证数; trust=compute_trust_score()
    if count >= threshold and trust >= threshold:
        UPDATE level + INSERT knowledge_promotions   ← 直接升级，无中间状态
```

| 维度 | 人类审批 | 蜂群晋升 |
|---|---|---|
| 触发 | 提交申请 | 治理循环定时扫描 |
| 判定 | 上级判断（人） | 阈值规则（确定性） |
| 状态 | "待审批"队列 | 无中间状态，直接升级 |
| 反例 | 驳回通知 | 自动降级 |

cross_validate 不是审批——是**事后投票**：只调整 trust 数值，不阻塞不等待。
投票影响"下次扫描是否够阈值"，不是"批准这次晋升"。

### 延迟分析

- **流程上零延迟**：批量扫描 500 条/60s，每条 ~3 SQL，全库几万条 < 1 秒；
  确定性判定，无任何人响应依赖
- **数据等待（真正的延迟）**：
  1. 60s 批处理延迟——设计容忍的异步性
  2. **印证数等待**：level 2 需 ≥1 独立来源，无 agent 复现则永远停 level 1——
     等的是证据不是批准，时长由冗余度决定（蜂群 vs 单 agent 的本质差异：
     单 agent 知识永远 level 1，蜂群靠冗余自动爬升）

### 为什么不用 LLM 审批

LLM 审批：秒级延迟 + token 成本 + 随机性（不可预测/不可复现）。
规则晋升：O(1) 确定性 + 可审计（knowledge_promotions 留痕）+ 零成本。

stigmergy 哲学延伸：**信任不来自权威，来自冗余**。知识对错交给规则+证据；
唯一用 LLM 判决的是 Controller（worker 的 kill/boost/spawn 资源调度，需全局视野）。

**一句话**：DIKW 晋升 = 阈值规则 + 证据积累，无审批人、无队列、无阻塞；
唯一可能的慢是"等证据"，证据速度 = 蜂群冗余度。
