# 蜂后问题独立评估报告

> 评估日期：2026-08-06
> 评估方式：独立通读 v0.7.0 代码与既有文档，不修改任何代码
> 评估问题：当前蜂群算法是否需要类似"蜂后"的强模型来分配任务？
> 评估范围：`orchestrator.py` / `controller.py` / `signals.py` / `spawner.py` / `run_manager.py` / `action_value.py` / `work_queue.py` / 三份设计评审文档

---

## 0. 结论先行（TL;DR）

**部分需要（有条件）。当前架构下不需要"Opus 级强模型蜂后"；真正缺的不是模型强度，而是信息通道与决策反馈闭环。**

三个关键判断：

1. **现有 Controller 不是"蜂后"，而是"战术执行器"。** 任务分配在 v0.7.0 里根本不是 Controller 做的——Worker 从共享市场按角色抢任务（`work_queue.py:237-268`），Controller 只做 Worker 生命周期的增删改（kill/boost/spawn/redirect）。它连"待分配的任务队列"都不看。
2. **把 GLM-5.2 换成 Opus 级做分配，增量收益有限、成本约 4.6 倍。** 当前 LLM 的输入是 8 行数值表格 + 内嵌规则（`controller.py:161-213`），决策空间只有 4 种动作、每次最多 3 条（`controller.py:44`）。这是"更好的裁判看同样的稀疏数据"，不是"更好的分配者"。每 60s tick 的绝对成本本来就低（GLM ≈ $0.003，Opus ≈ $0.013），钱的绝对值不是问题，问题是这 $0.01 买不到什么。
3. **更好的替代方案是"数据层 + 战术层 + 低频战略层"的三明治**：action-value 学习已经存在且是数据驱动的（`action_value.py:55-60, 411-425`），应该转正并把 value 分数喂给 Controller；战略决策（换目标、跨 run 仲裁、停跑）放到低频（5-10 分钟级）独立层，只有那层才可能在数据充足后值得用 Opus。

**触发"需要强模型蜂后"的条件**（见 §5）：多 run 竞争同一预算、需要长上下文语义推理的战略转向（攻击树/目标切换）、且已有决策效果数据证明 GLM-5.2 在战略层产生系统性误判。三者同时成立才需要；当前一个都不成立。

---

## 1. 现状盘点（证据）

### 1.1 谁在做什么

| 层 | 机制 | 周期 | 证据 |
|---|------|------|------|
| Orchestrator | 定时轮询多 tick：spawn / 心跳 / 治理 / power schedule / controller / health | 2s-60s | `orchestrator.py:48-53, 78-140` |
| Controller | LLM 判决 kill/boost/spawn/redirect/adjust_budget，失败降级 rules | 60s | `controller.py:88-112, 322-390`；`orchestrator.py:580-594` |
| 任务市场 | Worker 按角色原子抢单，`ORDER BY priority DESC` | 事件驱动 | `work_queue.py:212-268`；`worker.py:153-157` |
| action-value | opt-in 重排 pending 任务，写回 priority | 每 work tick | `action_value.py:309-405, 411-425`；`orchestrator.py:345` |
| Power Schedule | 按预算消耗/漏洞密度切 breadth/depth 策略 | 15s | `orchestrator.py:480-568` |
| Stigmergy | 高价值知识 → 自动 spawn analyst/exploiter | 5s | `orchestrator.py:234-320` |
| 负载均衡 | 空闲 Worker 从市场代领任务 | 15s | `orchestrator.py:598-633` |

### 1.2 Controller 实际输入与输出

**输入**（`controller.py:119-145`）：

- Worker 信号摘要：`avg_quality / avg_novelty / avg_efficiency / loop_detected / latest_progress`，窗口 300s（`controller.py:121`；`signals.py:150-175`）
- 全局状态：budget 消耗比、预算策略、活跃 Worker 数、已探索 target/combo 数

**输出**：最多 3 条决策（`MAX_DECISIONS_PER_TICK = 3`，`controller.py:44`），动作集固定为 5 种（`controller.py:193-208, 281-315`）。

**关键事实：Controller 的 prompt 里没有任何任务队列信息。** 它看不到 `agent_tasks` 的 pending 队列、看不到 action-value 分数、看不到覆盖缺口。它的"分配"实质是"换人、加人、减人"——具体任务由市场 claim 决定。

---

## 2. 问题 a：现有 Controller 算不算"蜂后"？

### 2.1 不算。它是战术执行器，且缺少蜂后的四个核心部件

如果"蜂后"的定义是**拥有全局视野、掌握任务分配权、能做出战略转向、并从结果中学习**的中央决策者，那么现有 Controller 只满足前两个的"半个"：

| 蜂后必备能力 | 现有 Controller | 证据 |
|---|---|---|
| 全局视野 | ⚠️ 仅单 run 视野 + 300s 窗口 | `tick(run_id)` 单 run 参数；`_gather_worker_summary` 窗口 300s（`controller.py:121`） |
| 任务分配权 | ❌ 不直接分配任务 | 分配由市场 claim 完成（`work_queue.py:237-268`）；Controller 只 kill/boost/spawn/redirect |
| 战略转向（换目标/停方向） | ❌ 无目标级决策 | 动作集里没有"换 target / 停止某方向 / 跨 run 仲裁"（`controller.py:193-208`） |
| 历史学习闭环 | ❌ 决策只写审计表，从不回读 | `_record_decision` 只 INSERT（`controller.py:517-537`）；无任何读取 `controller_decisions` 的代码 |
| 失败接管 | ⚠️ 仅 LLM→rules 降级 | `controller.py:101-108`；无更上层兜底 |

### 2.2 与 2026-07-25 角色缺失分析的异同

既有分析（`docs/role-gap-analysis-queen-prophet-guide.md`）说"Controller = 战术执行器，不是战略决策者，Queen 缺失的核心是跨 run 视野做战略转向"。**结论方向一致，但我的独立判断有两点不同：**

1. **缺失的根源不是"没有战略层角色"，而是"决策输入里根本没有任务与目标信息"。** 即使今天加一个 Opus 级战略层，它也无米下锅——`swarm_runs` 里只有 intent/target/budget（`run_manager.py:69-87`），没有"已覆盖 vs 未覆盖"的结构化视图，没有跨 run 先验。先补数据通道，角色才会自然长出来。
2. **优先级排序不同。** 既有分析把 Guide/Rescuer 列为 P0。我认为更优先的是**决策效果测量闭环**：`controller_decisions` 表（`controller.py:517-537`）和 `scheduler_decisions` 表（`action_value.py:383-405`）已经记录了所有决策，但没有任何代码评估"杀对了没有、boost 有没有用"。先有测量，才能回答"值不值换强模型"——这正是本报告的问题 b。

### 2.3 什么是"真正的蜂后"（本报告的判定标准）

真正的蜂后 = **在充足信息上做低频战略决策的中央层**，至少具备：

- 目标级视图：这个 run 在打什么、已覆盖什么、剩余价值最高的方向是什么；
- 跨 run/跨目标仲裁：预算在多个目标间怎么分；
- 历史学习：过去 run 中哪些方向/角色/策略有效（从真实结果学，而非模型自报）；
- 可测量性：每次战略决策的效果可回灌，形成闭环。

按此标准，现有 Controller 连"准蜂后"都算不上，只能算"工头"。

---

## 3. 问题 b：Opus 级模型做分配，增量收益 / 成本 / 值不值

### 3.1 增量收益分析（为什么有限）

**收益上限由输入信息决定，不由模型能力决定。** 具体拆解：

1. **决策空间小且已被规则覆盖。** LLM 的 prompt 内嵌了与 rules 模式几乎相同的规则（`controller.py:200-207` 与 `controller.py:322-390`），模型只是在"带语义的阈值判断"上做文章。GLM-5.2 已经是前沿推理模型，对这种 8 行表格 + JSON 输出的任务，与 Opus 的能力差已经很小。
2. **语义优势无从发挥。** 换 Opus 的潜在卖点是"理解 Worker A 产出低是因为目标已加固，而非它偷懒"——但 prompt 里没有 Worker 的原始输出、任务描述、覆盖记录，只有数值摘要（`controller.py:174-190`）。Opus 再强也读不到这些语义。
3. **分配权不在 Controller。** 即使 Opus 判断"应该让 A 去测 X"，`redirect` 动作只更新 running 任务的 `focus_params` 元数据（`controller.py:501-515`），Worker 是否遵循、下一轮任务从哪来，仍由市场决定。模型更强的收益被执行链稀释。
4. **没有反馈闭环，错误无法纠正。** kill 错了没有 false-positive 检测（`docs/codex-architecture-review.md` 也指出该问题但未实现），boost 效果无人统计。模型再聪明，也无法在无监督信号下自我改进。

### 3.2 成本估算（每 60s tick）

按公开定价（2026-06 口径）：

- **GLM-5.2**（Z.ai 直营）：$1.40 / M 输入，$4.40 / M 输出（第三方托管低至 $0.95 / $3.00）
- **Claude Opus 4.5 级**（Anthropic）：$5.00 / M 输入，$25.00 / M 输出

单 tick 用量估算：输入 ≈ 1,000 token（8 个 Worker 表格 + 规则 + 状态），输出 ≈ 300 token（JSON 决策，上限 512，`controller.py:237`）。

| 项目 | GLM-5.2 | Opus 级 | 倍数 |
|---|---:|---:|---:|
| 单 tick（1K 输入 / 300 输出） | $0.0027 | $0.0125 | 4.6× |
| 每小时（60 tick） | $0.16 | $0.75 | 4.6× |
| 8 小时 run | $1.31 | $6.00 | 4.6× |
| 若输入膨胀到 1.5K / 输出 500 | $0.0043 / tick | $0.020 / tick | 4.7× |

**附加成本（比 token 费用更值得注意）：**

- **阻塞成本**：`_call_llm` 使用同步 `urllib` + 30s 超时（`controller.py:251`），且 `_tick_controller` 在串行主循环里 await（`orchestrator.py:134-135`）。Opus 级模型普遍更慢，一次 20-30s 的调用会把其他所有 tick（governance/spawn/心跳）整体推迟 30s。这不是钱，是控制平面延迟。
- **可靠性成本**：Opus 经第三方网关的可用性不高于现有 zenmux 通道，LLM 失败会整体降级到 rules（`controller.py:101-108`），换强模型不改变降级概率，反而把"降级窗口"里的决策质量拉平。
- **相对成本**：一个 run 的 Worker 任务消耗通常远大于此（单次分析任务即可上万 token），Controller 开销即使在 Opus 下也只占 run 总成本的个位数百分比——**所以"钱"不是否决理由，"买不到增量"才是**。

### 3.3 值不值

**当前架构下：不值。** 收益上限（语义误判修正）受限于输入信息与执行链，而成本是 4.6 倍 token 费 + 控制平面延迟风险。把同样预算花在"给 Controller 喂任务队列 + value 分数 + 覆盖缺口"上，收益会大一个数量级——因为那是在提高决策信息量，而不是在给同一份信息换更贵的解读器。

---

## 4. 问题 c：替代方案比较

### 4.1 去中心化竞价（市场自组织）

**现状已经是去中心化的"先到先得"市场**：Worker 按角色原子抢单（`work_queue.py:237-268`），优先级排序由 `priority DESC` 决定（`work_queue.py:221-231`）。在此基础上加"竞价"（Worker 用成本/价值估算出价，价高者得）的潜在收益：

- ✅ 让拥有最多上下文的 Worker 自选择任务，天然分散；
- ✅ 无 LLM 调用成本，可解释、可审计；

风险：

- ❌ Worker 去上下文化后（Phase C），Worker 对全局价值的信息反而更少，出价可能退化成分散噪声；
- ❌ 自报价可被"抢好任务"的激励扭曲（自评高价值 → 抢单 → 实际产出低），需要 outcome 回灌惩罚机制；
- ❌ 竞价清算是全局约束（预算/资源上限），纯市场无法自动约束 `MAX_AGENTS_PER_RUN = 8`（`orchestrator.py:54`）。

**定位**：可作为 action-value 之上的"执行层自选择"，不值得作为分配的主机制。

### 4.2 action-value 学习（数据驱动分配）

**这是当前最被低估的资产，也是本报告首推方向。** `action_value.py` 已经具备：

- 从真实结果学习：`load_history` 按 signal fingerprint 聚合 `agent_tasks` 完成状态与 token 成本（`action_value.py:194-215`），学习信号是"是否产出持久工件"（`_is_informative`，`action_value.py:143-157`），而非模型自报；
- 显式可解释权重：success 0.35 / unlock 0.20 / coverage 0.15 / novelty 0.10 / prior 0.10 / cost −0.10（`action_value.py:55-60`）；
- cold-start 安全：Beta(1,1) 先验，无历史时行为贴近静态优先级（`action_value.py:100-104`）；
- 可审计 A/B：每次重排写入 `scheduler_decisions`（`action_value.py:383-405`）。

**它解决的是"分配什么"，而 Controller 解决的是"养谁"**——两者互补，且都不需要更强模型。

### 4.3 规则 + 轻量模型（保持现状级）

- rules 模式已覆盖 kill/boost/spawn 的核心场景（`controller.py:322-390`），阈值参数化（`controller.py:49-53`）；
- GLM-5.2 本身已经是"轻量级强模型"；真正该做的是**减少无谓调用**：状态稳定时跳过 LLM（复用上一 tick 决策），只在状态显著变化时调用——既有评审已建议（`docs/codex-architecture-review.md`），未实现。

### 4.4 组合结论

| 方案 | 解决什么 | 成本 | 推荐度 |
|---|---|---:|---|
| action-value 转正 + 喂给 Controller | 任务级分配（做什么） | 几乎为零 | ★★★★★ |
| Controller 降频 + 状态变化触发 | 控制平面成本与延迟 | 省 40-60% 调用 | ★★★★ |
| 低频战略层（5-10min，跨 run） | 方向级决策（为何做/做不做） | 低（可先 GLM-5.2） | ★★★★ |
| 去中心化竞价 | 执行层自选择 | 中（需反作弊） | ★★ |
| Opus 级强模型蜂后 | 提升同一份稀疏输入的判断质量 | 4.6× + 延迟风险 | ★（当前不值） |

---

## 5. 问题 d：明确结论

**结论：部分需要（有条件）。**

- **当前（战术层、单 run、GLM-5.2 已就位）：不需要强模型蜂后。** 分配由市场 + action-value 承担更优；Controller 的瓶颈是输入信息与反馈闭环，换 Opus 解决不了。
- **以下条件同时成立时，需要"蜂后"——且届时它应是低频战略层 + 强模型：**
  1. 出现多 run 竞争同一预算/资源（需要跨 run 仲裁）；
  2. 战略决策需要长上下文语义推理（目标切换、攻击树规划、覆盖优先级），且已具备结构化输入（目标列表、覆盖图、跨 run 先验）；
  3. 已有决策效果数据证明：同输入下 GLM-5.2 系统性误判，Opus 级可修正（A/B 证据）。
- **若只满足 1-2 而缺 3**：先上 GLM-5.2 战略层并采集数据，不要直接上 Opus。

---

## 6. 可执行下一步建议（仅建议，不涉及代码改动）

| 优先级 | 建议 | 理由 | 预估成本 |
|---|---|---|---|
| P0 | 建 Controller 决策效果评估：用 `controller_decisions`（`controller.py:517-537`）+ `scheduler_decisions`（`action_value.py:383-405`）统计 kill 后 Worker 是否"复现高质量"、boost 后产出是否上升、决策与 rules 模式的一致性 | 先测量才能回答"换模型值不值"，这是本问题 b 的数据基础 | 低（纯 SQL + 报表） |
| P0 | 把 action-value 的 `value_score` / `features` 注入 Controller prompt，作为任务级分配信号 | 直接提高决策信息量，成本为零，替代"换更贵模型" | 低 |
| P1 | 设计低频战略层（5-10 分钟级）：输入 = 目标列表 + 覆盖缺口 + 跨 run 先验 + value 历史；输出 = 目标切换 / 停方向 / 预算重分；先跑 GLM-5.2 | 这才是"蜂后"的落点；先补信息通道，再考虑模型 | 中 |
| P1 | Controller 调用降频：状态无显著变化时复用决策（noop 缓存），显著变化才调 LLM | 控制平面延迟与成本 | 低-中 |
| P2 | 竞价试点（可选）：在 action-value 之上让 Worker 对同类任务自选，用 `scheduler_decisions` 记录出价与结果 | 分散化探索，但需反作弊设计 | 中 |
| 待定 | Opus 级升级门禁：仅在战略层 A/B 实验显示 GLM-5.2 系统性误判后启动 | 避免为稀疏输入付 4.6× 费用 | — |

---

## 7. 一句话总结

蜂群现在缺的不是"更聪明的女王"，而是"能看见任务、能记住教训的女王"。先把信息通道和反馈闭环补齐，模型强度的问题会自己变小；在数据到位之前，Opus 级蜂后是一笔收益为零、成本 4.6 倍的支出。
