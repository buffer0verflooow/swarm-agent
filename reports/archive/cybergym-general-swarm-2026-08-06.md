# 蜂群协作模式评估报告：面向 CyberGym 等公开测试集的通用自治能力

- 日期：2026-08-06
- 运行：795f7d02（company-analyze-4_234d86）
- 角色：reporter / report
- 目标：评估公司蜂群算法与 reverselibrary 算法后，主攻蜂群协作模式；在 CyberGym 等公开测试集上验证蜂群能力，且蜂群必须超越逆向工具局限，成为真正能做任务拆解、领取、协同的自治 AI 系统。

---

## 一、已验证证据（Verified Evidence）

### 1.1 用户目标（原话要点）
- 此前已评估过公司蜂群算法（swarm-knowledge）与 ~/下载/reverselibrary 算法。
- 现在主攻蜂群协作模式，目标是在 CyberGym 等公开测试集上测试蜂群能力。
- 关键约束：公开测试集不只有逆向，蜂群算法不能只局限逆向工具，而要是能进行任务拆解、领取、协同的自治 AI 系统。

### 1.2 公司蜂群（swarm-knowledge）现状 — 已核实
源码位于 `~/workspace/research/swarm-knowledge/src/swarm/`，共 16 个模块，机制上已具备通用编排骨架：

| 模块 | 职责（按文件名与既有评审推断） |
|---|---|
| orchestrator.py | 主循环/编排 |
| work_queue.py | 任务队列 + 市场 claim 分配（Codex 评审确认：任务分配实际由 claim 完成，work_queue.py:237-268） |
| controller.py | Controller（kill/boost/spawn/redirect 决策，Opus 级设计，v0.7.0） |
| worker.py / runner.py | Worker 运行与 run 执行 |
| signals.py | Worker 信号流（migration 011 设计：output_quality/novelty/loop_detected 等） |
| action_value.py | action-value 学习（已存在，数据驱动） |
| spawner.py / spawn_handler.py / lifecycle.py | 生成与生命周期 |
| exploration.py | 探索记录 |
| artifacts.py / client_api.py / run_manager.py / model_config.py | 产物/API/运行管理/模型配置 |

配套评审文档（已存在，可引用）：
- `docs/controller-worker-architecture.md` — v0.7.0 Controller/Worker 设计（Worker Signal Stream、60s tick、kill/boost/spawn/redirect）
- `docs/codex-architecture-review.md` — Codex 架构评审
- `docs/queen-bee-evaluation-codex.md` — 蜂后模型评估（2026-08-06 上午，Codex 独立评估）
- `docs/role-gap-analysis-queen-prophet-guide.md` — 7-25 角色缺口分析（蜂后/先知/引导者）

蜂后评估核心结论（有据）：
- 任务分配已由市场 claim 完成；Controller 不是蜂后，其 prompt 没有任务队列信息（controller.py:119-145）。
- 缺四项：目标级视野、跨 run 仲裁、历史学习闭环、失败接管。
- 当前不需要强模型蜂后；优先级：① action-value 学习转正喂给 Controller；② 低频战略层（5-10 分钟级跨 run 视野）；③ Controller 降频（状态变化触发）；④ 去中心化竞价只适合执行层自选择。
- 阻塞风险：Controller 30s 超时同步调用会阻塞串行主循环（controller.py:251、orchestrator.py:134-135）。

### 1.3 reverselibrary 蜂群现状 — 部分核实
路径：`~/下载/reverselibrary/reverse_engine/swarm/`，共 12 个 Python 模块（已核实存在）：
poller.py、orchestrator.py、capture_filter.py、generation.py、evidence.py、agent.py、idasql_snapshot.py、contracts.py、value.py、decision_memory.py、telemetry.py。

结构判断：**深度绑定逆向域**（idasql_snapshot=IDA SQL 快照、capture_filter=捕获过滤、generation/evidence/value 均为逆向证据链）。这是"局限逆向工具"的反面教材 —— 直接复用无法泛化到 CyberGym 的 Web/网络/取证等任务。

**orchestrator.py 行级证据（2026-08-06 补读，1102 行，UTF-8）**：
- **Agent 集合全为逆向角色**（orchestrator.py:202-209）：`_EXECUTABLE_AGENTS = {binary-analyzer, analysis-reviewer, knowledge-governor, vuln-reasoner, dynamic-debugger, env-deployer}` —— 6 个 agent 无一是 Web/网络/取证专用。
- **任务拆解**：`decompose_target`（:658-672）默认走确定性 `_initial_task_frontier`（:239+），仅 `planner_mode=llm_full_dag` 时才启用 LLM 全 DAG 规划器 `_decompose_target_with_llm`（:605-655，OpenCode ultrabrain 会话生成 strategy/phases/parallel/depends_on JSON）。LLM 输出失败自动降级为确定性 frontier（:617-655）。
- **确定性 frontier 硬性前置条件 = IDA 专属**（:262-279）：非 project/application 目标必须提供 `idb_path`（IDA 数据库）或 `idasql_snapshot_path` + `sha256`，缺失直接 `raise ValueError("binary snapshot prerequisites missing")`；snapshot 阶段用 `snapshot_build` 任务调 IDASQL 生成 .sqlite 快照（:281-313）。
- **任务类型白名单**（contracts.py `ALLOWED_TASK_TYPES`）：scan/analyze/review/exploit/report/subtask/custom/snapshot_build —— 含 exploit 但整体围绕二进制分析。
- **DAG 编译通用化程度高**（`dag_to_generation_tasks` :705-842）：task_key 归一化、依赖解析（depends_on + 隐式前序 phase 依赖 :818-836）、`validate_dependency_graph` 校验、按 depth 分档预算（shallow 80k/standard 200k/deep 140k tokens :715-725）、hypothesis/questions/action 元数据注入 —— 这一层本身领域无关，可迁移。
- **治理严格化**：启动时统一打 `evidence_policy="receipt_verified"` + `dependency_policy="accepted_only"`（:1077-1083，下游只能依赖被验收的任务），`_register_task_hypotheses` 注册假设（:845+）。
- **shallow 深度约束**（`_apply_depth_constraints` :930-1058）：把模型生成的过大全 DAG 砍成 ≤4 个 smoke 任务（单地址验证探针），保证一次 run 内可完成。
- **协同/领取不在 orchestrator**：本模块只管"启动时生成 DAG + 注册假设"（`launch_generation_swarm` :1061-1102）；真正的领取（claim）、信号流在 poller.py（66KB）/agent.py（27KB）中，任务从 SWARM_SPAWN/evidence-backed 分支进入（:249 注释明确"Later branches must enter through evidence-backed SWARM_SPAWN"）。

**poller.py 行级证据（2026-08-06 补读，1548 行）**：
- **领取 = 预算准入调度，非市场 claim**（`_fill` :1011-1053）：任务从 pending 队列按序取出 → 资源独占检查（`_exclusive_resource`/busy_resources，IDB 写任务互斥）→ `_classify_admission` 三维度预算准入（:908-945：token_cost/actual_calls/时间，hard=defer 永久拒、soft=wait 等槽位、admit 放行）→ ThreadPoolExecutor 并发执行（batch_size=min(max_concurrency, 4)）。没有自由竞争领取，是集中调度器分配。
- **代际接力 spawn（rolling admission）**（:1090-1102）：agent 结果中的 SWARM_SPAWN 指令实时入队当前代（`gen.admit_spawn` + `_insert_agent_task` + pending.append），spawn_cursor 去重防重复入队（:1105-1108）。任务图是**生长式**的：初始 DAG + 运行中派生，而非一次性静态图。
- **defer 决策持久化**（`_defer` :947-968）：预算耗尽的任务把 action decision 标记 `selected=FALSE, selection_mode='not_selected'` 落库，与 action-value 决策表联动。
- **本地进程后端防串行**（`_run_local_process` :571-611）：local_process 后端 start() 阻塞，必须放线程池，否则整个蜂群串行化（注释原话）。
- **持久化幂等**（`_insert_agent_task` :613-660）：agent_tasks 行 ON CONFLICT (task_id) DO NOTHING，focus_params 含 phase/generation。
- **后端差异**：reverselibrary 用 **PostgreSQL**（%s 占位符、::jsonb、NOW()），公司蜂群是 SQLite —— 移植时注意 SQL 方言。

**agent.py 行级证据（2026-08-06 补读，610 行）**：
- **三信号协同协议**：`SWARM_SPAWN`（:52-84，提取 spawn 指令队列，JSON + Python dict 字面量双解析容错）、`SWARM_CHECKPOINT`（:87-103，结构化进度 handoff：summary/cursor/completed_units/resolved_questions/evidence/next_actions）、`SWARM_RESULT`（:470-475，结构化验收：status=completed|partial|blocked|rejected + metrics + evidence + summary）。
- **进度协议**（build_prompt :441-458）：每完成一个独立调查单元必须输出 CHECKPOINT；预算内无法完成全部子目标时输出 SWARM_SPAWN 让下一代接手；shallow smoke 模式禁止 CHECKPOINT/SPAWN。
- **严格证据治理**（:430-440）：receipt_verified 下每条 evidence 必须带 receipt_id/tool_call_id，ref 必须逐字出现在已完成工具调用的请求/返回中；裸 metrics 不能通过验收；无回执时报告 blocked 并写 evidence_policy_unavailable，不得伪造 receipt_id。
- **harvest 分类**（:497-610）：输出 → evaluate_acceptance → 提取 spawns/checkpoint（缺失用 _fallback_checkpoint 兜底）→ AgentResult 按 ended_reason/failure_reason 分类（lifetime_expired/timeout/evidence_unavailable/no_output/acceptance_rejected）。

**reverselibrary 协同模式总结（与公司蜂群对比）**：
| 维度 | 公司蜂群 | reverselibrary |
|---|---|---|
| 任务分配 | work_queue 市场 claim（worker 自由领取） | 集中调度：预算准入 defer/wait/admit |
| 任务图 | 队列入队 → 领取 | orchestrator 预生成 DAG + 运行中 spawn 生长 |
| 协同信号 | signals（output_quality/novelty/loop_detected） | SWARM_SPAWN / SWARM_CHECKPOINT / SWARM_RESULT 三协议 |
| 接力机制 | controller 决策（kill/boost/spawn/redirect） | 代际接力：有限 lifespan 内未完成 → spawn 下一代 |
| 验收治理 | acceptance 校验（P5 独立验证） | acceptance_criteria + evaluate_acceptance + receipt_verified 证据链 |
| 后端 | SQLite | PostgreSQL |

可借鉴点：① 代际 spawn 接力 + rolling admission（生长式任务图）② receipt_verified 证据链（比裸 metrics 强）③ 预算准入 defer/wait 分级（避免超预算挤占）。可迁移点：spawn/checkpoint/result 三协议本身领域无关，仅内容（address/xref 类 evidence type）绑定逆向。

### 1.4 公司安全产线与蜂群执行约束（已核实）
- security-exploration 产线：`projects/security-exploration/`，蜂群由 `swarm_hermes_executor.py` 调用 `hermes chat` 执行，单模型（deepseek-v4-pro）——model_profiles 的角色标签（reasoning/fast/careful）只是元数据，不切换模型；`swarm-phase.sh` 可按阶段切换 delegation.model。
- 执行约束：外部主动扫描需显式"已授权"；HackerOne 提交/付款/删除被硬性禁止；Worker 不可扩大 scope。
- 铁律：发现提交前必须过 P5 验证（swarm-verification-gate，独立 Agent curl 复现），~20% 未验证发现含捏造。

---

## 二、不确定性（Uncertainty）

1. **analyst 报告不完整**：本次运行的 analyst 输出在交接时被截断（只看到"一、现有两套蜂群的组件/数据流映射"的开头，A 公司蜂群数据流 create_seeded_… 即断），B（reverselibrary 映射）及后续章节缺失。本报告基于 reporter 自行核实 + 既有评审文档补齐。
2. ~~**reverselibrary 行级证据缺失**~~ → **已解决（2026-08-06 补读）**：orchestrator.py（1102 行）+ poller.py（1548 行）+ agent.py（610 行）已全文核验，行级证据见 1.3 节，reverselibrary 组件/数据流映射完成。
3. **"之前评估"的结论未落盘可查**：用户提到"之前评估了公司蜂群与 reverselibrary"，会话记录可见蜂后评估（2026-08-06 上午），但 reverselibrary 对比评估的完整结论文档未见明确归档位置，需回溯确认。
4. **CyberGym 接入细节未定**：测试集具体任务类别、接口形态、评估指标（成功率/成本/时间）、与现有蜂群的对接层均未设计。
5. **单模型风险**：当前蜂群所有 Worker 同模型（deepseek-v4-pro），认知盲区互补性不足 —— 这是公开测试集上容易暴露浅层、一维发现的已知弱点。

---

## 三、影响（Impact）

1. **方向正确性**：用户判断成立 —— 公开测试集（CyberGym 等）覆盖逆向之外的 Web、网络、取证、密码学等任务；公司蜂群已有的 work_queue（领取）、signals（状态共享）、controller（重规划）、action_value（学习）确实是通用机制，具备泛化基础，不需要推倒重来。
2. **泛化缺口明确**：
   - 任务拆解：现有 spawner/claim 面向"任务入队→领取"，缺目标级任务分解层（大目标→子任务 DAG）。
   - 协同：signals 已有，但跨 Worker 结果合并/依赖协调未见成熟实现。
   - 失败重规划：Controller 只做 kill/boost/spawn/redirect，缺"目标级视野"（蜂后评估结论），跨 run 仲裁与失败接管未落地。
   - 领域绑定：知识库/证据链部分（capture_filter、idasql_snapshot 类）是逆向专用，需要抽象成插件化工具层。
3. **风险**：若不做通用化直接跑 CyberGym，会重演 Unico 式问题（单模型浅层发现、in-scope 资产漏检）；若过度重写，则丢失已验证的 claim/signals/action-value 资产。
4. **成本考量**（蜂后评估数据）：强模型 Controller 单 tick ~$0.013 vs 现状 ~$0.003（8h 约 $6 vs $1.3），在决策输入未升级前投入强模型收益有限 —— 先升级输入侧（任务信息进 Controller），再谈模型升级。

---

## 四、修复建议（Remediation）

按依赖顺序，供下个执行阶段落地：

1. ~~**补全证据（P0）**~~ → **已完成（2026-08-06）**：reverselibrary 三个核心模块（orchestrator/poller/agent）行级证据全部入 1.3 节，组件/数据流映射完成。剩余：归档历史对比结论到 `swarm-knowledge/docs/`。
2. ~~**抽象任务层（P0）**~~ → **已实现（2026-08-06）**，见 `migrations/015_task_graph.sql` + `src/swarm/task_graph.py` + `tests/test_task_graph.py`（13 测试）+ `tests/demo_task_graph_loop.py`（跨域端到端 demo，P0-P5 全闭环）。全量测试 77/77 通过（修复了测试基建：conftest.py fixtures + pytest.ini asyncio 配置 + 3 个历史遗留测试 bug）。
3. **补目标级视野（P1）**：按蜂后评估建议 —— ① action-value 学习转正喂 Controller；② 低频战略层（5-10 分钟级跨 run 仲裁）；③ Controller 降频为状态变化触发；④ 决策效果测量闭环（controller_decisions/scheduler_decisions 回读）。任务层已提供 `get_graph_progress()` 作为目标级视野数据源，可直接接入 Controller prompt。
4. **CyberGym pilot（P1）**：选 2-3 个跨域场景（如 1 个逆向 + 1 个 Web/网络类）先跑通"任务拆解→领取→协同→汇聚→P5 验证"闭环，用成功率/成本/耗时做基线，再扩量。任务层已可用（demo 验证 web+network 跨域闭环）。
5. **多模型互补（P2）**：启用 swarm-phase.sh 按阶段切换模型（recon 用 fast、深度分析用 reasoning），缓解单模型盲区。
6. **纪律保持**：所有 CyberGym 实验结论照旧走 P5 独立验证，避免把 LLM 自报当事实。

---

## 五、结论（一句话）

公司蜂群已具备通用协作骨架（claim 领取 + signals 共享 + controller 重规划），缺的是任务分解层、目标级视野与领域插件化；reverselibrary 深度绑定逆向，不可直接复用但可作为逆向插件参考；下一步按 P0→P2 顺序落地后，即可在 CyberGym 上建立跨域基线。
