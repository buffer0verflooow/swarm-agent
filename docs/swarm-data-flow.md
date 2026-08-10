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
