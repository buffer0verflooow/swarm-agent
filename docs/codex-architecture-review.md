# Swarm Controller/Worker v0.7.0 架构评审报告

> 评审日期：2026-07-13  
> 评审方法：逐文件阅读 + Codex CLI 辅助分析  
> 审阅文件：`docs/controller-worker-architecture.md`、`src/swarm/controller.py`、`src/swarm/signals.py`、`src/swarm/orchestrator.py`、`migrations/011_worker_signals.sql`、`migrations/012_controller_decisions.sql`

---

## 1. 架构总体评价

### 1.1 设计优点 ✅

**三层分离清晰，渐进式复杂度控制**

架构文档 (`controller-worker-architecture.md` L54-L174) 将系统拆为三个独立的 Layer：

- **Layer 1 (Phase A)** — Worker Signal Stream，负责数据采集和量化
- **Layer 2 (Phase B)** — Controller LLM，负责决策
- **Layer 3 (Phase C)** — Worker 去上下文化，负责隔离

这种分层在实现中得到了忠实贯彻。`signals.py` 是纯数据层，`controller.py` 是决策层，`orchestrator.py` 通过 `_tick_controller()` (L547-L563) 做编排粘合，三个模块的职责边界清晰。

**LLM + 规则双模降级机制**

`controller.py` L101-L108 的设计非常务实：

```python
try:
    if self.mode == "llm":
        decisions = await self._tick_llm(run_id, worker_summary, global_state)
    else:
        decisions = self._tick_rules(run_id, worker_summary, global_state)
except Exception as e:
    _log.warning("Controller: LLM call failed, falling back to rules: %s", e)
    decisions = self._tick_rules(run_id, worker_summary, global_state)
```

LLM 调用失败时自动降级为规则模式，且规则模式的阈值 (`controller.py` L49-L53) 是独立可调的：`RULE_KILL_QUALITY=0.25`、`RULE_BOOST_QUALITY=0.70`、`RULE_SPAWN_WHEN_FEWER_THAN=2`。这种"fail operational"策略在不可靠的实验网络环境（架构文档 L240 提到 OhMyGPT 间歇故障）中至关重要。

**信号质量的三维量化**

`signals.py` 的设计用了三个正交维度来评估 Worker 产出：

| 维度 | 计算方法 | 代码位置 |
|------|---------|---------|
| `output_quality` | `QUALITY_BY_KNOWLEDGE_TYPE` 映射 + agent 自报 | `signals.py` L46-L54, L59-L81 |
| `novelty_score` | content hash 去重 + token overlap 相似度 | `signals.py` L261-L341 |
| `efficiency` | findings / (tokens_spent + 1) 或 knowledge_type 价值密度 | `signals.py` L363-L368 |

三维独立计算的优点是：即使某一个维度被"作弊"（比如 Agent 自报高 quality），其他维度（hash 去重）仍能独立约束。文档 L243 也明确指出了"信号自报不可信问题"，但实现中已经用 `compute_novelty_score()` (L261) 做了 content hash 交叉验证。

**原地打转检测的滑动窗口设计**

`signals.py` L197-L208 的 `detect_loops()` 使用滑动窗口而非简单的"最近 N 条"：

```python
for i in range(len(signals) - consecutive + 1):
    window_signals = signals[i:i + consecutive]
    if all(s["novelty_score"] < threshold for s in window_signals):
        ...
```

这比简单的"最后 3 条"更鲁棒——即使中间插入了一条短暂的高 novelty 信号，只要存在任意连续的 3 条低 novelty 窗口，就会被检测到。

**实验性决策 cap 防止失控**

`controller.py` L44 (`MAX_DECISIONS_PER_TICK = 3`) 和 `orchestrator.py` L219 (`MAX_AUTO_SPAWN_PER_TICK = 3`) 形成双重防护——Controller 每次最多 3 个决策，Stigmergy 自动 spawn 每次最多 3 个。这防止了"连锁 kill 导致 swarm 团灭"或"正反馈 spawn 导致预算耗尽"的雪崩场景。

**审计回溯能力**

`migrations/012_controller_decisions.sql` L8-L43 的 `controller_decisions` 表记录了每次判决的完整上下文：输入快照（`budget_remaining`、`stuck_workers`）、判决详情（`decision_type`、`reason`、`confidence`）、LLM 元数据（`llm_model`、`llm_raw_response`）。这为事后分析"Controller 为什么杀错了 Worker"提供了可追溯性。

**Worker 去上下文 + Stigmergy 自动发现闭环**

`orchestrator.py` L226-L296 的 `_tick_stigmergy_spawn()` 实现了"发现 → 自动 spawn → 分析 → 再发现"的正反馈循环。当 knowledge_entries 中出现 `vulnerability` 或 `level >= 3` 的 observation 时，自动生成 analyst/exploiter spawn 请求。且 `_active_stigmergy_spawn_exists()` (L297-L322) 通过 `dedup_key` + `json_each` 双重去重，避免了重复 spawn 同一目标。

---

### 1.2 脆弱点 ⚠️

**1. Controller 默认使用规则模式而非 LLM 模式**

`orchestrator.py` L555：

```python
ctrl = Controller(self.db, mode="rules")  # 默认规则模式，LLM 可选
```

架构设计文档 (L94) 的核心前提是「Controller 不是 Python 规则 — 是一个 LLM 调用」。但生产代码中硬编码了 `mode="rules"`。这意味着：

- **Controller 的"自适应决策"能力被阉割了**——规则模式 (`controller.py` L322-L376) 只有线性的 if/else，无法理解"Worker A 在扫描 API keys 但产出很低因为目标已经加固了"这种语义级判断。
- **没有切换回 LLM 的机制**——Orchestrator 创建 Controller 时传入 `mode="rules"`，即使 LLM 服务恢复也不会自动切回。

**2. `compute_novelty_score()` 的 token overlap 方法过于粗糙**

`signals.py` L306-L341 使用简单的 set intersection 计算 token overlap：

```python
tokens = set(content.lower().split()[:100])
snippet_tokens = set(snippet.split())
overlap = len(tokens & snippet_tokens) / len(tokens)
```

这种方法的局限性：

- **100 个 token 上限** (L306)：长内容只取前 100 个 token，尾部大量新信息被截断
- **不支持相似语义**：`"SQL injection in login"` 和 `"SQLi found in auth endpoint"` token overlap 接近 0，但实际上高度相关
- **阈值硬编码** (L332-L341)：`>0.8 → 0.1`、`>0.6 → 0.3`、`>0.4 → 0.5`、`>0.2 → 0.7`、`else → 1.0`——这些阈值没有经过调参，且对所有安全领域一视同仁（扫描 SQLi 和扫描 XSS 应该有不同的重复判定标准）

**3. Rules 模式下 kill 决策的"杀后再评"逻辑有竞态**

`controller.py` L328-L340 中，stuck/dead workers 直接被 kill：

```python
if w.get("loop_detected") or w.get("is_stuck"):
    decisions.append(ControllerDecision("kill", ...))
elif w["avg_quality"] < RULE_KILL_QUALITY and w["signal_count"] > 3:
    decisions.append(ControllerDecision("kill", ...))
```

然后 L342-L352 boost high performers 时排除被 kill 的：

```python
killed = {d.target_agent_id for d in decisions}
for w in workers:
    if w["agent_id"] in killed:
        continue
```

问题：如果 Worker B 刚刚开始产出高质量结果（最后 2 条 signal quality 从 0.1 跳到 0.8），但前 6 条平均 quality 仍 < 0.25，规则模式会杀掉它——而此时 B 正在"转好"。架构文档 L241 提到了"错误杀 Worker"但缓解方案是"先 kill → 重新评估"——规则模式没有"重新评估"这一步，全靠下一个 tick 中 Controller 重新 spawn。

**4. Orchestrator 的 6 个 tick 共享同一个 `run_loop` 没有背压**

`orchestrator.py` L83-L137 中，6 个 tick（spawn、stigmergy_spawn、work_market、heartbeat、governance、controller）全在同一个 `while not self._stopped` 循环中串行执行，基础间隔 1 秒：

```python
while not self._stopped:
    now = time.time()
    if now - last_spawn >= POLL_SPAWN_SEC: ...
    if now - last_work >= POLL_WORK_SEC: ...
    ...
    await asyncio.sleep(tick_interval)
```

问题是：governance tick (`_tick_governance`, L392-L466) 包含 7 个子步骤（DIKW 提升 + 衰减 + 信息素衰减 + 策略蒸馏 + 验证 pipeline + Wisdom 蒸馏 + 本体发现），如果其中某个步骤耗时 30 秒，所有其他 tick 都会被推迟，Controller 的 60s 间隔变成 ~90s。没有 tick 优先级或超时机制。

**5. `_execute_kill()` 中 `commit()` 和后续操作间无事务保护**

`controller.py` L399-L423：

```python
def _execute_kill(self, run_id: str, d: ControllerDecision):
    self.db.execute("UPDATE agent_profiles SET status = 'deprecated' ...", (aid,))
    self.db.execute("DELETE FROM agent_heartbeats WHERE agent_id = ?", (aid,))
    self.db.execute("UPDATE spawn_requests SET status = 'rejected' ...", (aid,))
    self.db.execute("UPDATE agent_tasks SET status = 'pending' ...", (aid,))
    self.db.conn.commit()
```

所有 UPDATE/DELETE 操作在一个 `commit()` 中提交——如果第 3 条 UPDATE 失败，前 2 条已经生效且无法回滚。应该用 SQLite 的显式事务：`BEGIN` → 执行 → `COMMIT` / `ROLLBACK`。

**6. `get_all_worker_signals()` 的 SQL 窗口有 SQLite datetime 精度问题**

`signals.py` L157-L175：

```sql
AND ws.created_at >= datetime('now', '-' || ? || ' seconds')
```

使用字符串拼接构建 SQL 参数 `'-300 seconds'`。SQLite 的 `datetime('now', '-300 seconds')` 依赖于系统时钟，如果系统时间被调整（NTP 同步跳跃），窗口可能错误地包含或排除信号。

---

## 2. 缺失的边缘案例和故障模式

### 2.1 信号层面的边缘案例

| 案例 | 当前行为 | 风险 |
|------|---------|------|
| **Worker 持续高质量但只产出同一种漏洞** | `novelty_score` 会下降但不是 0，可能不会被 detect_loops 捕获 | Worker 在"伪高效"——产出 20 个 SQLi 但实际是同一个 payload 的变体 |
| **Worker 产出全空（空字符串 `content=""`）** | `compute_novelty_score()` L279-280 返回 0.0，但 `record_worker_signal()` 仍写入 | Controller 看到 `novelty=0` 可能误杀正常 Worker（空输出不代表没价值） |
| **Worker 间相互触发循环 spawn** | Stigmergy (L297-L322) 通过 dedup_key 去重，但不同 entry_id 可能触发同一个 role | 两个不同的 vulnerability 条目可能都触发 `spawn analyst`——系统会 spawn 两个叠加的 analyst |
| **所有 Worker 同时高质量 → Controller 只 boost 不 spawn** | `_tick_rules()` L364 `budget_ratio > 0.7` 才 switch depth，但"全员高质量"时不需要改策略 | 浪费了扩大战果的窗口——应该在全员高效时 spawn 更多 worker 而非等待 |
| **Worker 信号表无限增长** | `worker_signals` 表没有清理策略 | 长时间运行的 swarm（数小时）会积累数十万条信号记录，查询变慢 |

### 2.2 Controller 层面的边缘案例

| 案例 | 当前行为 | 风险 |
|------|---------|------|
| **LLM 返回"乱码"JSON** | `_parse_llm_response()` L267-L276 提取 `\[.*\]` 正则，失败返回 `[]` | Controller 静默跳过——该 tick 没有决策 → 错过关键的 kill/spawn 窗口 |
| **LLM 返回的 agent_id 不在 worker_ids 中** | L285 `if aid and aid in worker_ids` 过滤 | LLM 幻觉产生虚构 agent_id → 决策被丢弃 → 模型不自知 |
| **Rules mode 在所有 Worker kill 后 spawn 的 scanner 没有任务** | `_tick_rules()` L356-361 spawn scanner 但没有传入 context_entry_ids | 新 scanner 没有方向，可能从零开始扫描，浪费 budget |
| **Controller tick 和 PowerSchedule tick 冲突** | Controller 在 L555 用 `mode="rules"` 调整 budget strategy，PowerSchedule 在 L468-L529 也调整 budget strategy | 两个独立 tick 同时写入 `budget_strategy` → 可能发生竞态覆盖 |
| **`adjust_budget` 决策无法回滚** | `controller_decisions` 表 L29-L32 定义了 `rolled_back` 状态，但 `_execute_adjust_budget()` L455-L463 没有实现回滚逻辑 | 策略切换错误后无法恢复 |

### 2.3 Orchestrator 层面的故障模式

| 故障 | 表现 | 代码位置 |
|------|------|---------|
| **spawn_handler 是 None 时静默拒绝** | L176-L179 中 `mark_spawn_rejected(self.db, req["request_id"], "no_spawn_handler")` | 所有 spawn 请求被悄悄丢弃，用户/Controller 不自知 |
| **`_safe_tick()` 吞噬所有异常** | L145-L149 `except Exception: _log.exception(...)` 不重新抛出 | 某个 tick 持续失败（如 governance 模块 import 失败）时，Orchestrator 继续运行但功能退化——没有健康检查告警 |
| **`_tick_stigmergy_spawn()` 查询未考虑已有 spawn 中的同 entry** | L238-L247 查询 `knowledge_entries` 不考虑已有 `spawn_requests` 正在处理 | 同一个 vulnerability 被多次拾取——L297-L322 的 `_active_stigmergy_spawn_exists()` 只在 spawn 之前去重，但查询和去重之间有时间窗口 |
| **MAX_AGENTS_PER_RUN = 8 硬编码** | L52 `MAX_AGENTS_PER_RUN = 8` | 不同规模的 target 需要不同的 workers（小型 target 8 个太多浪费；大型 target 8 个不够） |

---

## 3. 优化建议

### 3.1 降低成本

**P0：Controller 切换到 LLM 模式 + 缓存决策**

当前 `orchestrator.py` L555 硬编码 `mode="rules"`。建议：

```python
# orchestrator.py
ctrl = Controller(self.db, mode="llm")  # 默认 LLM
```

但配合缓存策略：如果连续 3 个 tick 的 worker 状态无显著变化（所有 quality 波动 < 0.1），跳过 LLM 调用，复用上一个 tick 的决策。这可将 LLM 调用量降低 40-60%，因为安全扫描中大多数 tick 的局面是稳定的。

**P1：Worker Signal Stream 的写入合并**

当前每个工具调用后写入一条 worker_signal（架构文档 L26-33）。对于高频工具调用场景（如每轮 10+ 个 HTTP 请求），建议引入批量写入：累积 5 条信号或 30 秒后一次性 flush。将 SQLite 写入次数降低 80%。

**P1：新发现缓写（novelty cache）**

`compute_novelty_score()` 每次调用都查 `raw_agent_events` 表（`signals.py` L295-L302）。建议在进程内存中维护一个 `set` 作为 L1 novelty cache（最近 1000 个 content_hash），减少 90%+ 的 SQL 查询。

### 3.2 增加鲁棒性

**P0：Controller/PowerSchedule 的 budget_strategy 写入锁**

`controller.py` L455-L463 和 `orchestrator.py` L520-L528 都可能写入 `swarm_runs.budget_strategy`。建议：

```python
# 在 swarm_runs 表中加入 strategy_version 字段，使用乐观锁
UPDATE swarm_runs SET budget_strategy = ?, strategy_version = strategy_version + 1
WHERE run_id = ? AND strategy_version = ?
```

**P0：kill 操作使用显式事务**

`controller.py` L399-L422 应改为：

```python
def _execute_kill(self, run_id: str, d: ControllerDecision):
    self.db.conn.execute("BEGIN IMMEDIATE")
    try:
        # ... 所有 UPDATE/DELETE ...
        self.db.conn.commit()
    except Exception:
        self.db.conn.rollback()
        raise
```

**P1：token overlap 改为 minhash + LSH**

`signals.py` L306-L341 的 set intersection 方法替换为 datasketch MinHash：

```python
# 使用 128 个 hash 函数的 MinHash
# 估算 Jaccard 相似度 → 映射到 novelty_score
# 不要截断到 100 tokens，使用全文
```

好处：支持近似语义相似（Jaccard 0.8 以下视为不同），不受内容长度限制，常数时间查询。

**P1：健康检查端点**

在 Orchestrator 中加入 `_tick_health()` 函数，每 30s 检查：

1. 所有子模块（signals、controller、exploration、governance）是否可 import
2. Controller 最近一次成功 tick 是否超过 120s
3. `worker_signals` 表大小是否超过阈值（如 100K 行）
4. LLM API 是否可达（ping `/v1/models`）

任一失败 → 写入 `swarm_health_events` 表 + 发出告警。

**P2：Worker Signal Stream 自动清理**

`worker_signals` 表加入定期清理：已完成的 run 保留最后 1000 条，超过 24h 的 signal 压缩归档到 `worker_signals_archive` 表。

**P2：Controller noop 率监控**

记录 Controller 输出 `[]`（空决策）的 tick 比例。如果 `noop_rate > 80%`，考虑将 `POLL_CONTROLLER_SEC` 从 60s 提升到 120s 以省成本。

### 3.3 功能增强

**P1：Worker 差分启动（diff context）**

`_build_spawn_context_worker()` (`orchestrator.py` L683-L707) 当前只注入关联发现（L697-L705），没有告诉 Worker "什么已经被测试过了"。建议注入一个精简的"已测试路径清单"（而非完整的 exploration_traces），用 20 个 token 以内表达"这几个 endpoint 已经有 scanner 测过了，跳过"。

**P2：Controller 学习反馈环**

`controller_decisions` 表中已有 `false_positive` 状态（`migration 012` L31）。建议：

1. 在下一个 tick 中检测：被 kill 的 Worker 的后续信号是否证明杀错了
2. 如果杀错了（`quality > 0.7` 在 kill 后重新出现），自动标记 `status='false_positive'`
3. 将误判模式注入 LLM prompt 作为负样本

**P2：多 run 间 Worker 经验迁移**

当前 Worker 是 per-run 隔离的。建议为成功 Workers 保存"经验片段"——如在某个 target 发现漏洞的模式——并允许后续 run 的 Worker 通过 retrieval 访问历史经验。

---

## 4. 与已知多 Agent 模式对比

### 4.1 AWS Security Agent

AWS Security Agent 使用事件驱动的响应模式：安全事件 → Lambda 触发 → Agent 分析 → 响应。其架构特点是 **无状态、单事件、快速决策**。

Swarm Controller/Worker v0.7.0 与 AWS Security Agent 的核心差异：

| 维度 | AWS Security Agent | Swarm Controller/Worker |
|------|-------------------|------------------------|
| 触发模式 | 事件驱动（reactive） | 定时轮询（proactive probing） |
| 状态管理 | 无状态（stateless lambda） | 有状态（SQLite DIKW + worker_signals） |
| 决策模式 | 规则引擎（Deterministic） | LLM + 规则双模（Heuristic + AI） |
| Worker 数量 | 单个 agent 按需调用 | 并发 swarm（最多 8 个 worker） |
| 反馈循环 | 无（分析 → 响应 → 完成） | 有（stigmergy: 发现 → spawn → 再发现） |

**Swarm 的优势**：AWS 模式的单事件视角无法处理"这个 target 的安全态势整体如何"这样的全局问题——Controller 通过全局 state + worker summary (L128-L151) 做到了这一点。

**Swarm 的劣势**：AWS 模式的事件驱动保证了"有事件必响应"，而 Swarm 的轮询模型在 stop-the-world 场景（如 governance tick 卡住 30s）会错过时间窗口。

### 4.2 PentAGI

PentAGI 采用 Planner-Executor 模式：Planner 生成攻击树 → Executor 按树执行 → 结果反馈给 Planner 更新树。

| 维度 | PentAGI | Swarm Controller/Worker |
|------|---------|------------------------|
| 任务分解 | 攻击树（树形结构） | Stigmergy 信号（扁平 spawn） |
| 协调方式 | Planner 显式分配任务 | Controller 隐式调控（kill/boost/spawn） |
| Worker 自主性 | 低（按 planner 指令执行） | 高（Worker 去上下文，独立决策） |
| 预算控制 | Planner 集中分配 | Controller + PowerSchedule 双重调控 |
| 适用场景 | 已知目标的深度渗透 | 未知目标的广度侦察 + 定向深挖 |

**Swarm 的优势**：PentAGI 的 Planner 在目标不明确时会生成低质量攻击树，而 Swarm 的 stigmergy 机制 (`orchestrator.py` L238-L278) 让探索方向从实际发现中涌现，避免了"预设计划"的偏差。

**Swarm 的劣势**：PentAGI 的攻击树提供了"可解释的渗透路径"——安全分析师可以追溯从初始侦察到漏洞利用的完整因果链。Swarm 的扁平 spawn 模型丢失了这种因果追溯能力。`controller_decisions` 表 (migration 012) 部分弥补了这点，但不够——无法回答"Exploiter C 是因为哪个发现被 spawn 的？"（除非手动关联 `spawn_requests.context_entry_ids`）。

### 4.3 关键模式对比总结

| 模式 | Swarm v0.7.0 | 业界最佳实践 |
|------|-------------|-------------|
| **监控粒度** | 每 60s tick（coarse） | AWS Security Hub: 事件驱动（fine） |
| **容错策略** | LLM→Rules 降级 | PentAGI: Planner 重规划 |
| **资源限制** | MAX_DECISIONS_PER_TICK=3, MAX_AGENTS=8 | K8s HPA: 动态 scaling |
| **状态隔离** | Worker 去上下文 | AWS Step Functions: 显式状态传递 |
| **审计追溯** | ✅ controller_decisions 表 | AWS CloudTrail + PentAGI 攻击树日志 |

---

## 5. 优先级行动项

| 优先级 | 类别 | 行动项 | 理由 | 预估工作量 |
|--------|------|--------|------|-----------|
| **P0** | 🔧 修复 | Controller 切换到 LLM 模式 (`orchestrator.py` L555) | 架构设计以 LLM 为核心，当前规则模式违背设计意图 | 1 行改动 |
| **P0** | 🔧 修复 | Controller/PowerSchedule 共享 `budget_strategy` 加入乐观锁 | 两个 tick 可能竞态覆盖，导致策略抖动 | ~20 行 |
| **P0** | 🔧 修复 | `_execute_kill()` 使用显式事务 (`controller.py` L399-L422) | 部分执行可能导致数据不一致 | ~10 行 |
| **P1** | ⚡ 优化 | `compute_novelty_score()` token overlap 升级为 MinHash (`signals.py` L306-L341) | 当前方法精度不足以区分"变体重复"和"真正新发现" | ~50 行 |
| **P1** | ⚡ 优化 | 引入 novelty cache（进程内存 L1 cache）减少 SQL 查询 | 高频率 signal 场景下减少 90%+ 的 hash 查重查询 | ~30 行 |
| **P1** | 🆕 新增 | Orchestrator 健康检查 tick (`_tick_health()`) | LLM 不可用、模块 import 失败时无告警 → 功能退化不自知 | ~60 行 |
| **P1** | 🆕 新增 | Worker spawn 时注入精简的"已测试路径清单" (`orchestrator.py` L683-L707) | Worker 去上下文导致重复探索是已知风险（文档 L242） | ~30 行 |
| **P1** | 🔧 修复 | 所有 Worker kill 后 spawn 的 scanner 注入 context_entry_ids (`controller.py` L356-L361) | 无上下文的新 Worker 浪费 budget | ~10 行 |
| **P2** | 🆕 新增 | Worker Signal Stream 自动清理（24h+ 归档） | 长时间运行的 swarm 会积累数十万条记录 | ~40 行 |
| **P2** | 🆕 新增 | Controller noop 率监控 → 动态调整 tick 频率 | 稳定局面时减少 50% LLM 调用 | ~30 行 |
| **P2** | 🆕 新增 | Controller 学习反馈环（false_positive 检测 → prompt 注入） | 让 Controller LLM 从错误中学习，减少误杀率 | ~80 行 |
| **P2** | 📊 监控 | 加入 Worker 产出多样性指标（同类型漏洞产出频率） | 防止"伪高效"Worker（同一 payload 变体重复产出） | ~30 行 |
| **P2** | 📝 文档 | PentAGI 风格因果追溯：spawn_requests 链可视化 | Controller 审计表缺少因果链，难以回答"Exploiter C 因何被 spawn" | ~50 行 |

**总计预估工作量**：P0 (~30 行) + P1 (~180 行) + P2 (~230 行) ≈ 440 行改动 + 测试

---

## 6. 总结

Swarm Controller/Worker v0.7.0 的三层架构设计（Signal Stream → Controller 判决 → Worker 去上下文）在概念上是自洽的，且实现代码质量良好——尤其是双模降级机制（L101-L108）、滑动窗口 loop 检测（L197-L208）、和 Stigmergy 自动发现闭环（L226-L296）是亮点。

三个 **P0 级别问题**需要立即修复：

1. **Controller 默认用 rules 模式违背设计意图**（`orchestrator.py` L555）——一行改动
2. **budget_strategy 竞态写入**（Controller + PowerSchedule 冲突）
3. **kill 操作非事务性**——可能导致数据不一致

**P1 级别**的优化重点在 `compute_novelty_score()` 的精度提升和健康检查机制。

整体而言，该架构在不出上述 P0 问题的前提下，已经可以作为可靠的多 Agent 安全扫描基础设施。关键评估指标建议在修复后运行一次端到端 swarm（4 worker × 10 分钟）并统计：

- Controller 的 false_positive kill 率
- 新 spawn Worker 的首次有用产出延迟
- LLM vs Rules 模式的决策一致性（Kappa 系数）
