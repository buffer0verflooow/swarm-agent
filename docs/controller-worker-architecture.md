# Swarm Controller/Worker 架构设计 (v0.7.0)

## 问题陈述

当前 Orchestrator 是纯规则引擎，缺乏三个能力：
1. **工人产出质量判断** — 知道工人"活着"但不知道"在推进还是在兜圈"
2. **细粒度 budget 控制** — 只能整群切换策略，不能给 Worker A 加预算同时干掉 Worker C
3. **工人不对称信息** — Agent 知道蜂群全局状态，无法做"盲实验"

## 设计概览

```
┌───────────────────────────────────────────────────────────────┐
│                  Controller (Opus-level LLM)                  │
│                                                                │
│  输入: Worker Signal Stream + 全局 budget + KB 状态            │
│  决策: kill / boost / spawn / redirect / adjust_budget         │
│  输出: caveman-wenyan 压缩 (省 65%+ 输出 token)                │
│  周期: 每 60s (对齐现有 governance tick)                       │
└──────┬────────────────────────────────────────────────────────┘
       │ 读取 signal stream    │ 决策写入 spawn_requests
       ▼                       ▼
┌──────────────────────────────────────────────────────────────┐
│              Worker Signal Stream (新增)                       │
│                                                                │
│  每个 Worker 每轮工具调用后写入:                                 │
│  - output_quality: 发现质量 (0-1)                              │
│  - novelty_score: 新发现 vs 重复 (0-1)                         │
│  - efficiency: 产出 / token 消耗比                              │
│  - loop_detected: 是否原地打转 (bool)                          │
│  - progress_marker: "scanned 5/20 endpoints"                  │
│  - last_useful_output_at: 上次有价值输出的时间                   │
│                                                                │
│  表: worker_signals (migration 011)                            │
└──────┬────────────────────────────────────────────────────────┘
       │ read                     │ write (Agent 自报 + 系统自动)
       ▼                          ▼
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│  Worker A   │  │  Worker B   │  │  Worker C   │
│  caveman    │  │  caveman    │  │  caveman    │
│  独立逆向    │  │  独立扫描    │  │  独立分析    │
│             │  │             │  │             │
│  不知道      │  │  不知道      │  │  不知道      │
│  被监控      │  │  被监控      │  │  被监控      │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       ▼                ▼                ▼
   capture.py      exploration_      knowledge_
   (knowledge_      trace.py          entries
    entries)        (exploration_     (DIKW)
                    traces)
```

## 三层改动

### Layer 1: Worker Signal Stream (migration 011)

```sql
CREATE TABLE worker_signals (
    signal_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    task_id         TEXT,
    signal_type     TEXT NOT NULL CHECK (signal_type IN (
        'tool_output', 'finding', 'progress', 'loop_detect', 'heartbeat'
    )),
    -- 产出质量 0-1 (LLM 估算或基于规则计算)
    output_quality  REAL DEFAULT 0.5,
    -- 新发现得分 (基于内容 hash 去重 + TF-IDF novelty)
    novelty_score   REAL DEFAULT 0.0,
    -- 产出/消耗比 (finding_count / tokens_spent_this_turn)
    efficiency      REAL DEFAULT 0.0,
    -- 原地打转检测
    loop_detected   INTEGER DEFAULT 0,
    -- 进度标记: "scanned 5/20 endpoints"
    progress_marker TEXT DEFAULT '',
    -- 上次产生有价值输出时间
    last_useful_at  TEXT,
    -- 原始数据
    raw_output_snippet TEXT DEFAULT '',
    metadata        TEXT DEFAULT '{}',
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_worker_signals_run_agent
    ON worker_signals(run_id, agent_id, created_at DESC);

CREATE INDEX idx_worker_signals_quality
    ON worker_signals(run_id, output_quality DESC);
```

### Layer 2: Controller (Opus-level LLM)

Controller 不是 Python 规则 — 是一个 LLM 调用，输入结构化信号，输出结构化决策。

```python
# src/swarm/controller.py (新增)

async def controller_tick(db, run_id: str) -> ControllerDecision:
    """Controller 每 60s tick — LLM 驱动的判决"""
    
    # 1. 收集所有 Worker 的最近信号
    signals = get_recent_worker_signals(db, run_id, window_seconds=120)
    
    # 2. 收集全局状态
    budget = get_budget_status(db, run_id)
    exploration = get_exploration_summary(db, run_id)
    
    # 3. 构建 LLM prompt
    prompt = build_controller_prompt(signals, budget, exploration)
    
    # 4. 调用 LLM (caveman-wenyan 压缩输出)
    decision_json = await call_llm(prompt)  # ← Opus 级模型
    
    # 5. 解析并执行决策
    decision = parse_controller_decision(decision_json)
    execute_controller_decision(db, decision)
    
    return decision
```

**Controller prompt 模板：**

```
你是蜂群控制器。审视以下 Worker 状态并做出决策。

全局状态:
- budget: 72000/100000 (72% spent)
- strategy: breadth → 应切换到 depth
- 已发现漏洞: 8 (2 HIGH, 6 MEDIUM)
- 已穷尽路径: 5

Worker 状态:
| # | role | progress | quality | efficiency | loop? | last useful |
|---|------|----------|---------|------------|-------|-------------|
| A | scanner | 8/20 endpoints | 0.7 | 2.3 | no | 30s ago |
| B | scanner | 2/20 endpoints | 0.1 | 0.2 | YES | 180s ago |
| C | exploiter | verifying FIND-03 | 0.8 | - | no | 45s ago |

可用动作:
- kill <agent_id>: 立即终止
- boost <agent_id> <reason>: 增加 budget 权
- spawn <role> <reason>: 生成新 Worker
- redirect <agent_id> <target>: 换方向

输出 JSON (caveman):
{"actions":[{"act":"kill","agent":"B","because":"180s no useful output, loop detected"},{"act":"spawn","role":"scanner","because":"replace B, continue unscanned endpoints"},{"act":"boost","agent":"A","because":"highest efficiency, near phase completion"}]}
```

### Layer 3: Worker 去上下文化

Worker Agent 启动时 **不再注入** 蜂群探索记忆和全局策略。

```python
# spawn_handler.py 改动

def _build_spawn_context_worker(self, req: dict) -> str:
    """Worker 专用 — 只给任务，不给全局状态"""
    parts = [f"## 任务\n{req['reason']}"]
    
    # 只注入任务相关的 KB 条目（触发发现），不注入全局探索记忆
    if entry_ids:
        for eid in entry_ids[:2]:
            row = self.db.fetch_one(...)
            if row:
                parts.append(f"\n### 发现: [{row['knowledge_type']}] {row['title']}")
                parts.append(row["content"][:300])
    
    # 注入 caveman 模式指令
    parts.append("\n## 输出模式")
    parts.append("使用 caveman-full 压缩输出。发现仍通过 capture.py 完整记录。")
    
    return "\n".join(parts)
```

## Caveman 集成点

| 位置 | Caveman 级别 | 省 token 量 | 说明 |
|------|-------------|-----------|------|
| Worker Agent 输出 | `full` | 65% output | 每个 worker 每条回复都压缩 |
| Controller LLM 调用 | `wenyan-full` | 80%+ output | 决策只需 JSON，文言文最大化压缩 |
| Controller prompt 注入 | `caveman-compress` | 46% input | 压缩 controller prompt 模板本身 |
| 蜂群 skill 文件 | `caveman-compress` | 46% input | 压缩 ~/skills/ 下的超大 SKILL.md |
| KB 上下文注入 | `caveman-compress` | 46% input | 压缩注入 Agent 的 KB 片段 |

**净收益估算（10 Worker × 20 轮 × 500 token 回复）：**

```
不用 Caveman:
  Worker output:  10 × 20 × 500 = 100,000 tokens
  Controller:     60 × 500         =  30,000 tokens (假设 60 轮)
  Skill injection:                  =  15,000 tokens
  Total:                          = 145,000 tokens

用 Caveman:
  Worker output:  100,000 × 0.35   =  35,000 tokens
  Controller:      30,000 × 0.20   =   6,000 tokens (wenyan)
  Skill (compressed): 15,000 × 0.54 =   8,100 tokens
  Injection cost (5K × 10):         =  50,000 tokens (一次性)
  Total:                          =  99,100 tokens
  
  省: 145,000 - 99,100 = 45,900 tokens (31.6%)
```

> ⚠️ 首次注入成本较高（每个 Worker 加 ~5K prompt tokens），但多次 Worker 回复后的压缩收益远超注入成本。

## 实施路线

### Phase A (本次) — Worker Signal Stream
- migration 011: `worker_signals` 表
- `src/swarm/signals.py`: `record_worker_signal()`, `get_recent_worker_signals()`
- 自动记录: Agent heartbeat 改为同时记录信号
- 原地打转检测: 连续 3 次 `novelty_score < 0.1` → 标记 `loop_detected=1`

### Phase B — Controller LLM
- `src/swarm/controller.py`: LLM 驱动判决
- `_tick_controller()` 加入 Orchestrator 循环
- 决策执行: kill Worker → `mark_spawn_rejected` / boost → 调高 priority / spawn → `request_spawn`
- 决策审计: `controller_decisions` 表记录每次判决

### Phase C — Worker 去上下文化 + Caveman 集成
- `spawn_handler` 支持 `mode='worker'` (去上下文)
- Worker prompt 注入 caveman 指令
- Controller prompt 使用 wenyan-full
- `caveman-compress` 压缩大型 skill 文件

## 与现有系统的关系

| 现有模块 | 变化 |
|---------|------|
| `orchestrator.py` | 新增 `_tick_controller()` (与 governance tick 同周期，不同协程) |
| `spawn_handler.py` | 新增 `_build_spawn_context_worker()` — 去上下文 |  
| `lifecycle.py` | heartbeat 增强 → 同时写入 `worker_signals` |
| `exploration.py` | 不变 — 探索记忆仍在，但 Controller 读、Worker 不读 |
| `capture.py` | 自动在 capture 后计算 `novelty_score` 写入 signals |
| `spawn_requests` | 新增 `controller_action` 字段 (标记决策来源) |

## 风险

1. **Controller LLM 调用成本** — 每 60s 一次 LLM 调用，需要 Opus 级模型。H1 实验环境可能不可靠（OhMyGPT 间歇故障）
2. **错误杀 Worker** — Controller 可能误判。缓解：先 kill → 重新评估（如果杀错了，spawn 同类 Agent 的请求会被自然生成）
3. **Worker 去上下文导致重复探索** — 没有探索记忆，Worker 可能重复测试。缓解：Controller 在 spawn 新的 Worker 前读 `exploration_traces` 判断是否需要
4. **信号自报不可信问题** — `output_quality` 和 `novelty_score` 由 Agent 自报或系统自动计算。系统自动计算更可靠（基于 content hash 去重 + TF-IDF）
