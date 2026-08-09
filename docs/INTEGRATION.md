# Swarm Knowledge Capture — 集成设计

> 知识不来自"提取"，来自"运行"。
> 每次 Agent 交互、每个任务结果、每条用户纠正，都是知识源。

## 一、架构

```
                       Agent 工作流
                            │
          ┌─────────────────┼──────────────────┐
          ▼                 ▼                   ▼
    任务完成            用户纠正            工具输出
          │                 │                   │
          ▼                 ▼                   ▼
    ┌─────────────────────────────────────────────┐
    │          CaptureContext (统一抽象)           │
    │                                             │
    │  source: task_result | user_correction | ... │
    │  content: 原始内容                           │
    │  source_agent: 谁产生的                       │
    │  metadata: 上下文信息                         │
    └────────────────────┬────────────────────────┘
                         │
                         ▼
    ┌─────────────────────────────────────────────┐
    │          capture() — 统一入口                │
    │                                             │
    │  ① is_worth_capturing() → 信号过滤          │
    │  ② classify_capture()   → 自动分类(DIKW)    │
    │  ③ enrich_with_context() → 关联已有知识      │
    │  ④ INSERT INTO knowledge_entries             │
    │  ⑤ 自动处理 counter_example / cross_validate │
    └────────────────────┬────────────────────────┘
                         │
                         ▼
                    SQLite (DIKW)
                         │
          ┌──────────────┼───────────────┐
          ▼              ▼               ▼
     治理引擎         本体引擎         聚类引擎
   (定时/触发)      (定时/触发)      (定时/触发)
```

## 二、集成点

### 2.1 Agent 任务完成时 (自动)

```python
from src.agents.capture import CaptureContext, CaptureSource, capture

# 在 swarm 编排器中，每个 agent 任务完成后:
def on_task_complete(task_result, swarm_db):
    ctx = CaptureContext(
        source=CaptureSource.TASK_RESULT,
        content=task_result.output,
        source_agent=task_result.agent_name,
        source_run_id=task_result.run_id,
        source_task_id=task_result.task_id,
        metadata={
            "task_type": task_result.task_type,
            "tool": task_result.tool_used,
            "exit_code": task_result.exit_code,
            "duration_ms": task_result.duration_ms,
        },
    )
    entry_id = capture(swarm_db, ctx)
    if entry_id:
        print(f"  📚 知识入库: {entry_id[:8]} L{ctx.metadata.get('level',1)}")
```

### 2.2 用户纠正 Agent 时 (自动)

```python
# 当用户说 "不对，应该是..." 时:
def on_user_correction(original_statement: str, correction: str, agent_name: str, swarm_db):
    ctx = CaptureContext(
        source=CaptureSource.USER_CORRECTION,
        content=f"原陈述: {original_statement}\n纠正: {correction}",
        source_agent=agent_name,
        metadata={
            "correction_type": "factual",
            "original_statement": original_statement,
        },
    )
    entry_id = capture(swarm_db, ctx)
    # 自动触发 counter_example 检查 → 可能衰减旧知识
```

### 2.3 错误被解决时 (自动)

```python
# 当 Agent 遇到错误并成功解决:
def on_error_resolved(error_message: str, solution: str, agent_name: str, swarm_db):
    ctx = CaptureContext(
        source=CaptureSource.ERROR_RESOLUTION,
        content=f"错误: {error_message}\n解决方案: {solution}",
        source_agent=agent_name,
        metadata={"error_type": _classify_error(error_message), "attempts": 3},
    )
    entry_id = capture(swarm_db, ctx)
    # 这类知识 level 较高 → 更容易被提升到 K/W
```

### 2.4 对话洞见 (半自动)

```python
# 周期性扫描对话历史:
def extract_from_conversation(messages: list, swarm_db):
    for msg in messages:
        if msg.role != "assistant":
            continue
        # 检测长推理输出
        if len(msg.content) > 300 and _has_concrete_finding(msg.content):
            ctx = CaptureContext(
                source=CaptureSource.CONVERSATION,
                content=msg.content,
                source_agent="system",
                metadata={"message_id": msg.id},
            )
            capture(swarm_db, ctx)
```

### 2.5 文章/文档 (显式触发)

```python
# 用户发文章时 (保持原有功能):
def on_article(text: str, url: str, title: str, swarm_db):
    # 先分块
    from src.agents.extractor import chunk_article, extract_knowledge_from_text
    entries = extract_knowledge_from_text(text, source_url=url, source_title=title)

    for e in entries:
        ctx = CaptureContext(
            source=CaptureSource.ARTICLE,
            content=e.content,
            source_agent="knowledge-extractor",
            metadata={"url": url, "title": title},
        )
        # 用 extractor 的分类结果覆盖自动分类
        entry_id = capture(swarm_db, ctx, auto_classify=False)
        # 手动设置分类
        if entry_id:
            swarm_db.execute(
                "UPDATE knowledge_entries SET knowledge_type=?, domain=?, level=?, tags=? WHERE id=?",
                (e.knowledge_type, e.domain, e.level, json.dumps(e.tags), entry_id),
            )
```

### 2.6 定时批量捕获 (cron)

```python
# 定期从最近的 swarm_runs 中批量捕获
from src.agents.capture import capture_from_run

def nightly_knowledge_harvest(swarm_db):
    """每晚运行: 从最近的 swarm_runs 中收集知识"""
    recent_runs = swarm_db.fetch_all(
        "SELECT run_id FROM swarm_runs WHERE ended_at > datetime('now', '-1 day')"
    )
    total = 0
    for run in recent_runs:
        result = capture_from_run(swarm_db, run["run_id"])
        total += result["captured"]

    # 然后运行治理周期
    from src.governance.engine import run_promotion_cycle, check_and_decay, run_full_clustering
    promoted = run_promotion_cycle(swarm_db)
    decayed = check_and_decay(swarm_db)
    clustered = run_full_clustering(swarm_db)

    return {"harvested": total, "promoted": promoted["promoted"],
            "decayed": len(decayed["decayed_entries"])}

# 用 Hermes cron:
# hermes cron create "0 3 * * *" "运行 nightly_knowledge_harvest"
```

### 2.7 外部 Agent 客户端调用蜂群

Claude、Hermes、Codex 或自定义执行器都只是蜂群的上游调用端：它们下发高层任务，拿到 `run_id`，再查询状态和结果。模型选择、任务拆分、worker 领取、capture、summary 都由蜂群内部完成。

```bash
# Hermes/Claude/Codex 下发一个高层任务
python3 scripts/swarmctl.py task submit \
  --source hermes \
  --task "对 example.test 做授权范围内的侦察和漏洞初筛，并输出可复现证据" \
  --intent recon \
  --target-type webapp \
  --target example.test \
  --json

# 查询任务状态
python3 scripts/swarmctl.py task status --run-id "$RUN_ID" --json

# 获取蜂群结果；未完成时返回当前摘要，完成后返回最终 result/summary
python3 scripts/swarmctl.py task result --run-id "$RUN_ID" --json

# 可选：等待完成
python3 scripts/swarmctl.py task wait --run-id "$RUN_ID" --timeout 300 --json

# 管理蜂群默认模型画像；这里把 analyst 默认配置为 claude/sonnet
python3 scripts/swarmctl.py models set \
  --role analyst \
  --provider claude \
  --model sonnet \
  --default
```

`scripts/agent_worker.py --claim-only/--complete-task-id` 是蜂群内部 worker 入口，不是 Hermes/Claude/Codex 的主调用入口。外部工具只需要 `scripts/swarmctl.py task submit/status/result/wait`。

如果没有配置具体 provider，默认 profile 使用 `provider=client`，表示蜂群只指定能力档位（例如 `scanner/fast`、`analyst/reasoning`）。真实执行器可以在蜂群内部把这些 profile 映射到 Claude、Codex 或本地模型。

## 三、信号过滤规则

| 来源 | 最小信号 | 说明 |
|------|---------|------|
| task_result | 0 | 任务结果总是值得保留 |
| user_correction | 0 | 纠正是最强的学习信号 |
| error_resolution | 0 | 错误解决方案可复用 |
| article | 0 | 用户主动发的内容 |
| tool_output | 1 | 需要一定的结构化内容 |
| conversation | 2 | 需要强信号（因果关系/数据/对比） |
| discovery | 0 | 系统发现总是可信 |
| cross_validation | 0 | 多 Agent 验证总是可信 |

信号计算:
- 包含具体数值 → +1
- 包含因果关系 → +1
- 包含工具/命令 → +1
- 包含 CVE → +2
- 包含对比判断 → +1
- 用户纠正 → +2
- 错误解决方案 → +2

## 四、与现有模块的关系

```
src/
├── db.py              # SQLite 封装
├── agents/
│   ├── scripts/      # CLI 入口 (capture/swarmctl/init_db/...)
│   └── extractor.py   #   文章提取 (保留, 作为 capture 的一个特例)
├── swarm/
│   ├── run_manager.py #   创建 run + 发布并行 seed tasks
│   ├── work_queue.py  #   共享任务市场: publish/claim/complete
│   ├── model_config.py #  蜂群自维护模型 profile + 对话 summary
│   ├── worker.py      #   Agent 运行循环: claim → execute → capture → complete
│   └── orchestrator.py #  市场维护 + spawn 扩容 + 治理 tick
├── governance/
│   └── engine.py      #   DIKW 提升 + 衰减 + 聚类
└── ontology/
    └── inference.py   #   本体发现 + 推理

工作流:
  scripts/swarmctl.py task submit/status/result
     → 外部工具下发任务、查询状态、获取结果

  scripts/start_swarm.py / scripts/swarmctl.py models
     → 创建 run、维护模型 profile、记录事件、生成 summary

  scripts/agent_worker.py / SwarmWorker
     → 蜂群内部 worker 入口
     → claim_work_tasks()
     → resolve_task_model_profile()
     → 执行任务
     → capture()
     → publish_tasks_for_knowledge()
     → complete_work_task()

  capture() → knowledge_entries → work market / governance / ontology inference
```

## 五、下一步

1. ✅ `scripts/capture.py` 已写
2. ✅ `work_queue.py` 已把 `agent_tasks` 变成共享任务市场
3. ✅ `worker.py` / `scripts/agent_worker.py` 已支持 claim → execute/manual complete
4. ✅ `run_manager.py` / `scripts/start_swarm.py` 已支持市场化 run 初始化
5. ✅ `model_config.py` / `scripts/swarmctl.py` 已支持蜂群自维护模型 profile 和 run summary
6. 🔲 在外部 swarm-hunt/Claude/Hermes/Codex 执行器中接入 worker loop
7. 🔲 在客户端中集成 `on_user_correction` 钩子
8. 🔲 实现 `nightly_knowledge_harvest` cron job
