# Swarm-Knowledge 架构成熟度评估报告

> 评估日期：2026-07-15
> 评估方法：只读检查（文档 + 源代码 + 数据库 + 测试结构），不修改任何文件
> 评估者：analyst-01（数据采集）→ reporter（汇总撰写）

---

## 1. 综合评分

| 维度 | 评分 | 评级 | 关键证据 |
|------|------|------|---------|
| 架构设计 | 4/5 | ★★★★☆ | 三层分离 (Signal→Controller→Worker)、DIKW 金字塔、Stigmergy 间接通信 |
| 代码质量 | 4/5 | ★★★★☆ | 类型注解、docstring、显式事务、P0 已修复 (2026-07-14 auto-fix) |
| 测试覆盖 | 3/5 | ★★★☆☆ | 5 个测试文件、57 个测试函数、但无端到端 LLM 真实测试、pytest 未安装 |
| 运维成熟度 | 2/5 | ★★☆☆☆ | DB 含僵死数据 (5 runs/5 profiles/6 spawns)、budget_spent 列缺失、无生产运行记录 |
| 文档质量 | 4/5 | ★★★★☆ | 5 份文档 (~1,397 行)、架构评审详实、代码内 docstring 完整 |
| 演化能力 | 4/5 | ★★★★☆ | 13 个版本化 migration、DIKW 自动提升、治理引擎自动蒸馏 |
| 生产就绪 | 2/5 | ★★☆☆☆ | datasketch 未安装 (MinHash 不可用)、pytest 不可执行、无安全加固 |
| **综合** | **3.3/5** | **★★★☆☆** | **设计良好但尚未就绪生产部署** |

---

## 2. 各维度详细分析

### 2.1 架构设计 ★★★★☆ (4/5)

**已验证的优点：**

1. **三层分离清晰** (`docs/controller-worker-architecture.md` L12-L52)：Phase A (Worker Signal Stream) → Phase B (Controller LLM 判决) → Phase C (Worker 去上下文化)。`signals.py` 是纯数据层、`controller.py` 是决策层、`orchestrator.py` 做编排粘合，职责边界清晰。

2. **LLM + 规则双模降级** (`src/swarm/controller.py` L101-L108)：LLM 调用失败时自动降级为规则模式，运维不可靠环境的核心容错能力。

3. **信号三维量化** (`src/swarm/signals.py`)：output_quality + novelty_score + efficiency 三个正交维度，任一维度"作弊"可被其他维度约束。

4. **滑动窗口 loop 检测** (`signals.py` L197-L208)：比"最近 N 条"更鲁棒——中间插入短暂高 novelty 不误判。

5. **决策 cap 双重防护**：`MAX_DECISIONS_PER_TICK=3` + `MAX_AGENTS_PER_RUN=8`，防止连锁 kill 或雪崩 spawn。

6. **Stigmergy 自动发现闭环** (`orchestrator.py` L226-L296)：Discovery → Auto-spawn → Analysis → Re-discovery 正反馈循环，探索方向从实际发现中涌现。

**已验证的风险：**

- Worker 去上下文导致重复探索 (文档 L242-243)
- 信号自报不可信 (文档 L243 — 已通过 content hash 交叉验证缓解)
- 扁平 spawn 模型丢失因果追溯能力 (对比 PentAGI 攻击树)

### 2.2 代码质量 ★★★★☆ (4/5)

**已验证：**

| 特征 | 位置 | 状态 |
|------|------|------|
| Controller LLM 模式 | `orchestrator.py:581` | ✅ `mode="llm"` 已生效 (原为 `mode="rules"`，2026-07-14 修复) |
| budget_strategy 乐观锁 (Controller) | `controller.py:480-498` | ✅ CAS with `strategy_version` |
| budget_strategy 乐观锁 (PowerSchedule) | `orchestrator.py:528-550` | ✅ CAS with `strategy_version` |
| kill 显式事务 | `controller.py:416-442` | ✅ BEGIN IMMEDIATE / COMMIT / ROLLBACK |
| MinHash novelty_score | `signals.py:446-483` | ✅ `_minhash_signature()` + `_jaccard_from_signatures()` |
| 健康检查 tick | `orchestrator.py:735-773` | ✅ `POLL_HEALTH_SEC=30`, `_tick_health()` |
| Worker spawn 注入上下文 | `controller.py:358-375` | ✅ 查询 `knowledge_entries` 作为 `context_entry_ids` |

**Git 提交记录** (`git log --oneline`):
```
6c8dd49 fix: auto-fix P0/P1 issues [codex-daily]           ← 2026-07-15 验证
855655d docs: add Daily Auto-Fix 2026-07-14 section
b2e0dcd fix: auto-fix P0/P1 issues from architecture review  ← 2026-07-14 主要修复
7cb36b5 feat: Worker 去上下文化 + Caveman 集成 (Phase C)
c102807 feat: Controller LLM 判决层 (Phase B)
9364e67 fix: detect_loops 滑动窗口 + capture 信号 efficiency 修复
1d17a5b feat: Worker Signal Stream (Phase A)
923e8af docs: Controller/Worker v0.7.0 架构设计
```

**未解决的脆弱点：**

- `compute_novelty_score()` 升级到 MinHash（`signals.py` 已有 `_minhash_signature()`）但依赖库 `datasketch` **未安装** — MinHash 路径实际不可用
- Orchestrator 6 个 tick 共享同一循环无背压 (`orchestrator.py` L83-L137)
- Rules 模式下 kill 决策的"杀后再评"有逻辑竞态 (架构评审 §1.2 第 3 项)

### 2.3 测试覆盖 ★★★☆☆ (3/5)

**测试文件清单：**

| 文件 | 测试函数数 | 覆盖范围 |
|------|-----------|---------|
| `tests/test_controller.py` | 7 | Controller kill/boost/spawn 决策 |
| `tests/test_worker_signals.py` | 8 | 信号记录、novelty 计算、loop 检测 |
| `tests/test_swarm_loop.py` | 29 | 蜂群完整循环 (最大覆盖) |
| `tests/test_exploration_traces.py` | 8 | 探索路径记忆 |
| `tests/test_worker_mode.py` | 5 | Worker 去上下文模式 |
| **合计** | **57** | |

**架构评审文档声称测试结果：**
```
test_controller.py:        14/14 passed ✅
test_worker_signals.py:    27/27 passed ✅
test_exploration_traces.py: 25/25 passed ✅
test_worker_mode.py:        9/9 passed ✅
test_swarm_loop.py:        ALL TESTS PASSED ✅
```
总声称用例数：75+

**实际可验证状态：**

- ❌ `.venv/` 不存在 — 虚拟环境未创建
- ❌ `pytest` 未安装到系统 Python — 无法执行测试
- ❌ 架构评审中的测试结果来自 Codex CLI 环境 (2026-07-14/15)，本机不可复现
- ⚠️ 测试函数数 (57) 与声称通过用例数 (75+) 不一致 — `test_swarm_loop.py` 的 29 个函数可能包含多场景参数化
- ❌ 无端到端 LLM 真实调用测试 — 所有测试使用嵌入式 SQLite

### 2.4 运维成熟度 ★★☆☆☆ (2/5)

**数据库状态 (swarm_knowledge.db):**

| 表 | 记录数 | 状态 |
|----|--------|------|
| `knowledge_entries` | 64 | 含历史探索数据 |
| `worker_signals` | 5 | 极少 (仅测试残留) |
| `swarm_runs` | 5 | **僵死数据** — 无活跃运行 |
| `agent_profiles` | 5 | **僵死数据** — 无活跃 Worker |
| `spawn_requests` | 6 | **僵死数据** — 全为 pending/rejected |

**发现的问题：**

1. 数据库含 5 个 swarm_run 但无任何活跃运行 — 历史残留未清理
2. 5 个 agent_profile 但无对应活跃 Worker — 同上
3. 6 个 spawn_request 处于悬空状态 — 同上
4. `swarm_runs` 表缺少 `budget_spent` 列 — budget 追踪断裂 (查询 `budget_spent` 直接报错 `OperationalError: no such column`)
5. 无生产级蜂群运行记录 (`git log` 无 run 相关日志)
6. 无监控/告警基础设施 (健康检查 tick 已实现但未验证运行)

### 2.5 文档质量 ★★★★☆ (4/5)

| 文档 | 行数 | 内容 |
|------|------|------|
| `docs/ARCHITECTURE.md` | 119 | 系统架构概览 |
| `docs/DESIGN.md` | 252 | 设计原则与决策记录 |
| `docs/INTEGRATION.md` | 283 | 集成指南 |
| `docs/controller-worker-architecture.md` | 243 | v0.7.0 架构设计 (三层方案、Caveman 集成、风险分析) |
| `docs/codex-architecture-review.md` | 500 | 架构评审 (6 节，含 Daily Auto-Fix 记录) |
| **合计** | **1,397** | |

**文档质量评价：**

- ✅ 架构评审 (`codex-architecture-review.md`) 质量高：设计优点 + 脆弱点 + 边缘案例 + 优化建议 + 横向对比，结构完整
- ✅ v0.7.0 架构设计文档 (`controller-worker-architecture.md`) 含三层方案、Caveman 成本估算、风险矩阵
- ✅ 代码内 docstring 完整 (抽查 `controller.py`, `signals.py`, `orchestrator.py`)
- ⚠️ 无 API 参考文档 (swarmctl.py 的命令行帮助可用)
- ⚠️ 5 份文档中 3 份较短 (< 300 行)，`ARCHITECTURE.md` 尤其概要

### 2.6 演化能力 ★★★★☆ (4/5)

**Migration 版本历史：**

| 编号 | 文件名 | 功能域 |
|------|--------|--------|
| 001 | `schema.sql` | 基础 schema |
| 002 | `swarm_extensions.sql` | 蜂群扩展 |
| 003 | `architecture_fixes.sql` | 架构修复 |
| 004 | `verification_wisdom.sql` | 验证 + Wisdom |
| 005 | `spawn_claims.sql` | Spawn 声明 |
| 006 | `work_market.sql` | 工作市场 |
| 007 | `model_profiles.sql` | 模型画像 |
| 008 | `raw_events_artifacts.sql` | 原始事件 + 制品 |
| 009 | `bounty_knowledge_loop.sql` | Bounty 知识循环 |
| 010 | `exploration_traces.sql` | 探索路径 |
| 011 | `worker_signals.sql` | Worker 信号流 |
| 012 | `controller_decisions.sql` | Controller 判决审计 |
| 013 | `budget_strategy_lock.sql` | 策略乐观锁 |

**演化特征：**

- ✅ 13 个版本化 migration，有序命名，可追溯
- ✅ 治理引擎支持 DIKW 自动提升 (`src/governance/engine.py`)
- ✅ 本体自动发现 (`src/ontology/discovery.py`)
- ✅ 策略蒸馏与 Wisdom 蒸馏 (`src/governance/wisdom.py`)
- ✅ Git 历史线性演进 (15 commits)，包含 docs/feat/fix 提交规范

### 2.7 生产就绪 ★★☆☆☆ (2/5)

**环境状态：**

| 检查项 | 状态 |
|--------|------|
| Python 3.14.4 可用 | ✅ |
| sqlite3 可用 | ✅ (纯 Python 标准库) |
| `.venv/` 虚拟环境 | ❌ 不存在 |
| `pytest` | ❌ 未安装 (系统 Python 无 pytest 模块) |
| `datasketch` | ❌ 未安装 (MinHash 功能不可用) |
| `numpy` | ❌ 未安装 |
| `networkx` | ❌ 未安装 |
| 生产运行记录 | ❌ 无任何活跃蜂群运行 |
| 安全加固 | ❌ 无认证/授权/加密机制 |
| 日志/监控 | ⚠️ 健康检查 tick 已实现但未验证 |

**关键阻塞因素：**

- **datasketch 缺失** — MinHash-based novelty score (`signals.py:446-483`) 引用了 `_minhash_signature()` 和 `_jaccard_from_signatures()`，但底层依赖 `datasketch` 库未安装。此功能是 v0.7.0 的核心升级 (替代粗糙的 token overlap)，缺失意味着 novelty 计算回退到原始方法或直接失败。
- **pytest 缺失** — 57 个测试用例全部无法执行，测试回归能力为零。
- **依赖断裂** — `requirements.txt` 声明的 `numpy>=1.24` 和 `networkx>=3.0` 均未安装到系统 Python。

---

## 3. 行动项汇总

### P0 — 阻塞生产部署

| # | 项 | 文件 | 影响 |
|---|----|------|------|
| 1 | 安装依赖 (pytest, numpy, networkx, datasketch) | `requirements.txt` | 测试不可执行、MinHash 不可用 |
| 2 | 创建 .venv 并安装依赖 | — | 环境不可复现 |
| 3 | 运行全量测试并修复失败 | `tests/` | 57 测试函数未经实际验证 |
| 4 | 修复 `swarm_runs.budget_spent` 缺失 | `init_db.py` / migration | budget 追踪完全断裂 |

### P1 — 影响功能完整性

| # | 项 | 文件 | 影响 |
|---|----|------|------|
| 5 | 清理 DB 僵死数据 | `swarm_knowledge.db` | 历史残留误导分析 |
| 6 | Orchestrator 引入 tick 背压/超时 | `orchestrator.py` | governance tick 卡住推迟整个循环 |
| 7 | Worker Signal Stream 自动清理策略 | `orchestrator.py` | 长期运行信号表膨胀 |

### P2 — 增强与完善

| # | 项 | 文件 | 影响 |
|---|----|------|------|
| 8 | 端到端 LLM 真实调用测试 | `tests/` | 无 LLM 集成测试 |
| 9 | 因果追溯 (spawn→findings 链) | `orchestrator.py` | 缺少 PentAGI 风格可解释链路 |
| 10 | 安全加固 (认证/授权) | — | 生产环境要求 |

---

## 4. 总结

Swarm-Knowledge v0.7.0 的**架构设计成熟度良好** (三层分离 + DIKW 金字塔 + Stigmergy)，**代码质量扎实** (P0 已修复、显式事务、乐观锁)，**文档完整** (1,397 行)。但是：

- **运维成熟度低** (2/5)：数据库含僵死数据、budget 追踪断裂、无生产运行记录
- **生产就绪度低** (2/5)：依赖未安装 (datasketch/numpy/networkx/pytest)、测试不可执行
- **测试覆盖中等** (3/5)：57 测试函数但无一可执行，无端到端 LLM 测试

**当前状态：适合继续开发，不适合生产部署。** 优先安装依赖并跑通现有测试后，可升级到 3.5/5 生产就绪门槛。

---

> 评估依据：本机 `/home/pwn/workspace/research/swarm-knowledge/` 目录只读检查
> 未修改：无文件被创建、编辑或删除 (除本报告写入)
> 数据源：Git log、SQLite DB、源代码、文档、migrations
