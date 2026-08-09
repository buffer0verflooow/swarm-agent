# Shared Signal Board（共享信号板）

**状态**：已实现（2026-08-07）· 模块 `src/swarm/signal_board.py` · 测试 `tests/test_signal_board.py`（7 项全过）

## 由来

MARBLE database 对照实验（见 `reports/marble-benchmark-evaluation-2026-08-06.md`）：
蜂群分工 v1（verifier 各自扫描信号源）只有 4/7 命中——视野窄导致误报/漏报，
且每个 verifier 重复扫描 pg_stat_statements 浪费 90% token。修复后 v2 达到
8/8 全中：**probe 一次性收集共享快照 → 所有 worker 基于快照判定 → lead 拿完整
证据汇聚**。本模块把这个模式从 benchmark 专用代码固化为核心机制。

## 模型

信号板是存储在 `task_graphs.metadata["signal_board"]` 的 JSON dict，**图级作用域、
追加式、按节点版本化**：

```
publish_signal()  ──►  board["probe_snapshot"] = {...}     # probe/采集节点写一次
attach_evidence() ──►  board["evidence"]["analyze:X"] = {...}  # 每个节点附证据
build_signal_context() ──►  worker 领取任务时自动注入上下文
collect_evidence() ──►  lead 汇聚完整证据做最终裁决
```

关键约束：**已发布信号不可被下游覆盖**（append-only，除非 collector 自己
`overwrite=True` 重跑）。任何 worker 都不能改写别人的发现——板子是可信共享内存。

## API

| 函数 | 作用 |
|---|---|
| `publish_signal(db, graph_id, key, value, overwrite=False)` | 发布结构化信号 |
| `get_signal(db, graph_id, key, default)` / `get_signals(...)` | 读取 |
| `attach_evidence(db, graph_id, node_key, evidence)` | 节点附完整证据（不覆盖） |
| `collect_evidence(db, graph_id)` | lead 取全部证据（完整，非摘要） |
| `get_graph_id_for_task(db, task_id)` | 任务 → 图（无图返回 None） |
| `build_signal_context(db, task, max_chars=4000)` | 任务上下文注入（图 goal + 信号板 + 兄弟节点证据） |

## 自动注入

`worker.build_task_context()` 已接入：任何 graph 附属任务被 worker 领取时，
信号板内容自动拼进任务上下文。对非图任务（legacy 流程）返回空串，零侵入。
注入失败静默降级（try/except），绝不影响任务领取。

## 标准用法（probe → verify → lead 三段式）

```python
from src.swarm.signal_board import publish_signal, attach_evidence, collect_evidence

# 1. probe 节点：采集一次，发布共享快照
snapshot = collect_signals()                 # 领域专用采集函数
publish_signal(db, gid, "probe_snapshot", snapshot, overwrite=True)

# 2. analyze 节点：worker 领取时 build_task_context 自动注入快照，
#    基于快照判定，完成后附证据
attach_evidence(db, gid, f"analyze:{rc}", {"present": True, "evidence": "..."})

# 3. synthesize 节点：lead 拿完整证据做全局裁决
evidence = collect_evidence(db, gid)
final = lead_aggregate(evidence, snapshot)
```

## 设计要点

- **图级作用域**：板子挂在 task_graphs.metadata，随图生命周期，不污染全局
- **append-only**：下游节点不能覆盖 probe 快照或兄弟证据 → 可信、可审计
- **完整证据汇聚**：lead 看到的是完整 evidence dict 而非截断摘要（对照实验证明
  截断证据是 lead 纠偏失败的主因之一）
- **判据化 verifier**：worker 的 STRICT EVIDENCE RULES（明确阈值）与信号板
  配合，verifier 从"各自扫描"变为"基于共享快照判定"，tool_calls 降 90%
- **零迁移**：复用 task_graphs.metadata JSON 列，无需新表

## 关联

- 前序：`migrations/015_task_graph.sql`、`src/swarm/task_graph.py`（P0 任务层）
- 消费方：`benchmarks/marble_db_runner.py --mode swarm`（MARBLE 适配层）
- 反模式对照：v1 verifier 各自扫描（信息丢失 + token 浪费）→ 本模块修复
