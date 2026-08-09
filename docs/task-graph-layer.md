# Task Graph Layer (P0) — 领域无关任务层

- 日期：2026-08-06（P0）；2026-08-07 更新（信号板 P1）
- 状态：已实现，测试通过（85/85）
- 关联：`migrations/015_task_graph.sql`、`src/swarm/task_graph.py`、
  `src/swarm/signal_board.py`、`tests/test_task_graph.py`、
  `tests/test_signal_board.py`、`tests/demo_task_graph_loop.py`

## 动机

CyberGym 等公开测试集覆盖逆向之外的 Web、网络、取证、密码学任务，公司蜂群
要成为真正的自治 AI 系统，任务模型不能绑定逆向域。本层在现有 work market
（`work_queue.claim_work_tasks`）之上提供**目标级任务分解**，同时保持与
signals / controller / action-value 完全兼容。

## 设计

```
task_graphs (目标级: goal/strategy/domain/统计 + metadata.signal_board)
   ├─ task_graph_nodes (DAG 节点: task_key/goal/depends_on/acceptance/tools/budget)
   │     └─ 发布为 agent_tasks 市场任务 (graph_id/task_key 关联)
   │           └─ worker 照常 claim → 执行 → complete_graph_task()
   │                 ├─ evaluate_acceptance()  验收标准 (metric/op/value/required)
   │                 ├─ task_evidence          证据链 (receipt-verified)
   │                 └─ spawn_subtask()        滚动接力 (rolling admission, 图生长)
   └─ signal_board (P1 共享信号板: metadata.signal_board)
         ├─ publish_signal()     probe 节点写共享快照 (append-only)
         ├─ attach_evidence()    每个节点附完整证据
         ├─ build_signal_context() worker 领取时自动注入
         └─ collect_evidence()   lead 汇聚全部证据裁决
```

关键属性：

1. **领域无关** — role / task_type / tool_allowlist 是纯字符串。代码中无任何
   IDA/二进制/扫描器概念。reverselibrary 的逆向工具集只是未来"插件"的一种。
2. **依赖门控发布** — `publish_ready_nodes()` 只发布 depends_on 全部
   completed 的节点；根节点先发，子节点随父节点完成逐波解锁。
3. **复用现有市场** — 节点发布为 agent_tasks 行，worker 的 claim/capture/
   signal 逻辑零改动。`complete_graph_task()` 同时更新节点状态与图统计。
4. **生长式任务图** — `spawn_subtask()` 在运行中把新节点加入图并立即尝试
   发布（父已完成则直接可领取），移植自 reverselibrary 的 rolling admission。
5. **证据链** — `task_evidence` 的 ref 必须逐字出现在工具回执的
   request/response 中才计为 verified；裸 metrics 不能单独通过验收。
6. **共享信号板（P1）** — 图级共享黑板，存于 `task_graphs.metadata`。
   probe/采集节点把结构化信号快照发布一次（append-only，不可被下游覆盖），
   worker 领取任务时经 `build_task_context` 自动注入，lead 汇聚完整证据做
   最终裁决。解决并行分工的"视野窄+重复扫描"问题（详见
   `docs/signal-board.md` 与下方"信号板"节）。

## API 摘要

| 函数 | 作用 |
|---|---|
| `create_task_graph(db, run_id, goal, ...)` | 建图 |
| `add_task_node(db, graph_id, task_key, goal, depends_on, ...)` | 加节点（校验依赖存在） |
| `publish_ready_nodes(db, graph_id)` | 依赖门控发布到市场 |
| `publish_graph_all(db, graph_id)` | 发布所有当前可发节点 |
| `evaluate_acceptance(db, task_id, result, criteria=None)` | 验收评估 |
| `record_task_evidence(db, task_id, type, ref, receipt_id)` | 记录证据 |
| `verify_evidence_receipts(db, task_id, tool_receipts)` | receipt-verified 校验 |
| `complete_graph_task(db, task_id, result)` | 完成/失败 + 图统计 |
| `spawn_subtask(db, parent_task_id, task_key, goal, ...)` | 滚动接力 |
| `get_graph_progress(db, run_id)` | 目标级视野（Controller 输入） |
| `mark_graph_completed(db, graph_id)` | 图完成判定 |

## 验收标准格式

```json
[{"metric": "output_nonempty", "op": "==", "value": true, "required": true},
 {"metric": "verified_evidence_count", "op": ">=", "value": 2, "required": true},
 {"metric": "note", "op": "contains", "value": "ok", "required": false}]
```

支持 op: `==` `!=` `>=` `<=` `>` `<` `in` `contains`；metric 支持点路径
（如 `report.count`）。注意：**不支持 `gte`/`exists` 等 SQL 风格 op**——
适配层曾踩坑（probe 节点验收一直 failed 的隐藏根因），一律用符号 op。

## 信号板（P1，2026-08-07）

由来：MARBLE database 对照实验证明，分工型蜂群若各 worker 自行扫描信号源，
会"视野窄"（误报/漏报）+ 浪费 token（重复扫描）。修复 = 共享信号快照 +
完整证据汇聚，蜂群从 4/7 提升到 8/8（反超单 LLM 的 6/7）。本模式已固化为
核心模块 `src/swarm/signal_board.py`。

模型：信号板是 `task_graphs.metadata["signal_board"]` 的 JSON dict，图级
作用域、追加式、按节点版本化：

| API | 作用 |
|---|---|
| `publish_signal(db, graph_id, key, value, overwrite=False)` | probe 写共享快照（append-only） |
| `get_signal / get_signals(db, graph_id)` | 读取信号 |
| `attach_evidence(db, graph_id, node_key, evidence)` | 节点附完整证据（不可覆盖） |
| `collect_evidence(db, graph_id)` | lead 汇聚全部证据（完整，非摘要） |
| `get_graph_id_for_task(db, task_id)` | 任务 → 图（无图返回 None） |
| `build_signal_context(db, task)` | 任务上下文注入（图 goal + 信号板 + 兄弟证据） |

**自动注入**：`worker.build_task_context()` 已接入——graph 附属任务被领取时
信号板自动拼进上下文；非图任务（legacy 流程）返回空串，零侵入；注入失败
静默降级，不影响领取。

**标准三段式**：probe 采集并 `publish_signal` → analyze worker 基于快照判定
并 `attach_evidence` → synthesize lead 用 `collect_evidence` 全证据裁决。
benchmark 侧只需提供领域采集函数（如 MARBLE 的 `collect_probe_snapshot`），
协作模式核心直接复用。

## 验证

- `tests/test_task_graph.py`：13 个测试（生命周期/依赖门控/领取/验收/证据/
  接力/进度）。
- `tests/test_signal_board.py`：7 个测试（发布/读取、append-only、证据汇聚、
  metadata 持久化、任务→图映射、上下文注入、legacy no-op）。
- `tests/demo_task_graph_loop.py`：跨域端到端 demo（web + network 双 scanner
  并行 → 依赖解锁 → 验收+证据 → 滚动接力 → 图完成），P0-P5 全闭环。
- 全量：`.venv/bin/python -m pytest tests/ -q` → **85 passed**。
- 真实蜂群端到端：`--mode swarm` 跑 MARBLE task 0 → F1=1.0，graph metadata
  中确认 `probe_snapshot` + 8 节点完整证据链持久化（可审计回放）。

## 与 P1 的衔接

- `get_graph_progress()` 即蜂后评估中"目标级视野"的数据源，可并入 Controller
  prompt（低成本、规则模式即可消费）。
- CyberGym pilot 可直接用本层建图：goal = 测试集任务描述，role/tool 按场景
  声明（如 `scanner`+`http` 或 `analyst`+`re_ida` 插件）。
