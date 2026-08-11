# 假设→验证→确认 管线核查报告（reporter）

- 报告人: reporter（蜂群 Worker, profile: default-reporter-writer）
- 日期: 2026-08-11
- 目标: 验证"蜂群探索产出假设 → 假设必须验证 → 只有通过验证的才是真实发现"这条闭环在 swarm-knowledge 代码库中的落地程度
- 方法: 只读本机代码（src/ + migrations/ + docs/ + tests/），每项声明附 文件:行号 证据；无外部主动探测（目标未授权，遵守约束 1/2）
- 结论概要: 闭环的**前半段已落地且被测试覆盖**（验证队列 + 自动入队/处理）；**后半段（假设门控 → validated）没有接入自动执行链**，且自动验证是启发式打分而非真实复现。见下表。本次合入知识 [adb8c6b1]（analyst-01 查询痕迹）后，G1/G3 升级为**数据级实证**（见 §6）。

---

## 1. 已确认的证据（✅ 代码级核实）

| # | 声明 | 证据 |
|---|------|------|
| V1 | 存在验证队列表 validation_queue（pending/assigned/validating/verified/refuted/timeout 状态机） | migrations/004_verification_wisdom.sql:9-29 |
| V2 | 自动入队: vulnerability 类（trust≥0.65）或 mechanism L3+ 或任何 L3+ 且未验证条目 → validation_queue，并**自动创建 finding_hypotheses** | src/governance/verification.py:44-133（尤其 :120-128） |
| V3 | 自动处理队列: `_auto_verify` 三重检查（交叉验证来源数 / 反例数 / 内容含可验证特征 IP·CVE·URL·工具名）→ 打分定 verdict（confirmed/refuted/inconclusive） | src/governance/verification.py:136-281 |
| V4 | confirmed → pheromone boost + trust +0.05；refuted → trust -0.20 + 反例记录；确认/反驳均回写知识条目 | src/governance/verification.py:186-198, 284-316 |
| V5 | 假设模型存在: finding_hypotheses（validation_status: hypothesis/validating/validated/refuted/negative_knowledge）+ 6 道报告门控（poc_exists / clean_repro / impactful / low_priv_reachable / in_scope / deduplicated） | migrations/009_bounty_knowledge_loop.sql:9-72；src/governance/bounty.py:16-33 |
| V6 | 门控聚合: 全部 pass/not_applicable → status=validated，知识条目 level 强制 ≥3 + trust 提升；任一 fail → negative_knowledge + trust -0.20 | src/governance/bounty.py:256-324 |
| V7 | 验证队列已接入 Orchestrator 60s 治理 tick | src/swarm/orchestrator.py:475-487（调用 auto_enqueue_validations + process_validation_queue） |
| V8 | 存在 P5 三层堆叠审查方法论（事实/上下文/价值）与"未验证发现 20% 含捏造"的实证教训 | skill `swarm-verification-gate`（P5a/P5b/P5c + KOHO Finding 6 案例） |
| V9 | worker 层产物验证（artifact 缺失 → 任务失败）已实现且有测试 | src/swarm/worker.py（verify_artifacts 调用点）；tests/test_swarm_loop.py:1265-1296 |
| V10 | 验证队列按 run 隔离、处理后不重复入队，有测试覆盖 | tests/test_swarm_loop.py:1442-1492 |

---

## 2. 不确定性 / 缺口（⚠️ 未验证或未闭环）

| # | 缺口 | 证据 | 性质 |
|---|------|------|------|
| G1 | **假设门控（V5/V6）没有接入自动执行链**。`record_gate_result` / `evaluate_hypothesis_gates` 全仓库唯一调用点是 docs/DESIGN.md 示例 与 tests/，scripts/ 与 orchestrator 均无调用 | search: record_gate_result 调用者 = DESIGN.md:168-173 + tests + src/__init__.py 导出；scripts/ 0 命中；**数据级实证（合入 [adb8c6b1]）**: 本机 KB finding_hypotheses 现有 10 条全部停在 validation_status='hypothesis'，0 条到达 validated/refuted/negative_knowledge（直查 swarm_knowledge.db，2026-08-11） | 闭环断裂：假设被创建后 validation_status 停在 hypothesis/validating，**永远不会自动到达 validated**（存量 10 条假设已实证卡死） |
| G2 | 自动验证是**启发式文本打分，不是真实复现**。`_auto_verify` 只做 lineage 计数 + 正则扫描（IP/CVE/URL/工具名），不执行任何外部请求验证 | src/governance/verification.py:255-281 | "通过验证"≠"真实"，与用户目标（验证过的才是真的）有偏差 |
| G3 | **inconclusive 被记成队列状态 'verified'**。三目表达式: confirmed→'verified'，refuted→'refuted'，**inconclusive→'verified'**（与 verdict 字段矛盾，队列状态语义失真） | src/governance/verification.py:178-179；**真实残留样本（合入 [adb8c6b1]）**: validation_queue 实测 1 条 status='verified' 但 verdict='inconclusive'（validation_id bba12172-9353-48c6-b21c-7d2d6f3bb0dc，2026-07-03 14:36:46，reason="有 1 个来源确认; 包含工具输出特征"），且该表 CHECK 约束不含 'inconclusive'（只允许 pending/assigned/validating/verified/refuted/timeout），bug 已造成污染数据入库 | 状态机 bug（已实证 + 有存量污染） |
| G4 | 自动验证信任库内信号，可被投毒放大。A13: 伪造 lineage（A3/A12 手法）→ 自动"确认" → trust 再提升 → 报告层引用 | reports/security-audit-2026-08-11.md:183-188（A13）、:174-181（A12） | 验证独立性不足 |
| G5 | 假设库盲区: 验证只在假设清单内有效，CWE 分类错位时分工验证反而漏（bounty_1 实证: 8 CWE verifier 全败） | docs/ARCHITECTURE-BLINDSPOTS.md:120-144 | 分析层覆盖天花板 |
| G6 | tool_policy（本 profile 声明 network=false/shell=false）**从不强制执行**，仅是元数据 | reports/security-audit-2026-08-11.md:157-162（A10） | 权限声明与实效不符 |

---

## 3. 影响评估

- 正面: V2-V4/V7 说明"探索 → 捕获 → 自动入队验证 → trust 更新"这条**知识侧**闭环真实运转且被 157 项测试覆盖（security-audit-2026-08-11.md:324），可防止低置信发现直接进 L3+。
- 负面:
  1. **假设生命周期半途而废**（G1）——finding_hypotheses 会堆积在 validating/hypothesis 状态，ROI 排序（rank_hypotheses_by_roi）永远只看到未决假设，"通过验证的才是真实发现"无法由系统自动兑现；
  2. **验证≠复现**（G2+G4）——"confirmed" 仅表示库内信号一致，不代表外部事实成立；投毒条目可被自动确认，等于把"验证"变成放大器而非过滤器；
  3. **状态语义失真**（G3）——inconclusive 记 verified，下游消费队列状态的地方会误判"已验证"。

---

## 4. 修复建议（remediation，按优先级）

1. **P0 — 把门控接入自动链**: 在 `process_validation_queue` 处理完后（或 orchestrator 治理 tick 内）对 confirmed 的 hypothesis 自动执行 `record_gate_result` 的机器可判门（in_scope/deduplicated 可查库判；poc_exists/clean_repro/impactful 降级为需人工/独立 agent 补证据），并调用 `evaluate_hypothesis_gates` 推进 validation_status → validated 或 negative_knowledge。目标: 闭环可自动走到 validated。
2. **P0 — 验证独立于库内信号**: 参照 swarm-verification-gate 的 P5a 思想，confirmed 判定要求真实外部复现（curl/工具回放），至少对 HIGH 条目强制；库内 lineage 仅作线索不作证据（修复 A13）。
3. **P1 — 修 G3 状态 bug + 清存量脏数据**: inconclusive → 队列状态保持 'inconclusive'（迁移 004 的 CHECK 需加该值）或回退 pending，禁止记 verified；同时清洗存量 1 条污染行（validation_id bba12172-9353-48c6-b21c-7d2d6f3bb0dc，status 改回 inconclusive 或 pending），并加回归断言防复发（见 §6.2）。
4. **P1 — G5 缓解**: 假设清单外保留 free-explore 通道（已部分存在），并在验证 prompt 中提示"分类可能错位，按代码形态而非 CWE 描述验证"。
5. **P2 — G6**: 强制执行 tool_policy 或移除误导性声明（与 A10 修复项一致）。
6. 每项修复后回归: `.venv/bin/python -m pytest tests/ -q`（当前基线 157 passed + 公司集成 8/8，见 security-audit-2026-08-11.md:324）。

---

## 5. 给下一环节的交接

- 建议下一步由 analyst 评审本报告的 G1 修复方案（门控自动化的判定规则边界），并由 P5 验证关对"inconclusive→verified"bug 做一次独立复现确认（读 verification.py:178-179 即可，无需外部目标）。
- 所有代码证据均为本次会话直接读取核实，未做任何写库/外部探测。

---

## 6. 高置信知识合入记录 [adb8c6b1]

- **知识条目**: `adb8c6b1-6665-4f94-b776-6757a96432b4`（knowledge_type=tool_usage, L3, source_agent=analyst-01, run 42a93f3d, trust_vector cross_validation=1.0, status=active, pheromone=1.0）
- **内容性质**: analyst-01 的 KB 查询脚本痕迹（kb_query_analyst.py / kb_query2.py / kb_query3.py 三份 review diff），非结论本身，而是**证据收集线索**：指向 validation_queue / finding_hypotheses / finding_validation_gates / negative_knowledge / task_evidence 五张表的直接查询路径。
- **合入方式（不盲信，独立复核）**: 以该条目的查询路径为线索，本报告独立直查 swarm_knowledge.db 复核，得出两条**数据级实证**，已回填到 §2 表格：
  1. **G1 数据确认** — `finding_hypotheses` 表现有 10 条假设，validation_status 全部停在 `hypothesis`，0 条到达 validated/refuted/negative_knowledge（2026-08-11 直查）。"假设永不自动达 validated"从代码推断升级为存量数据事实。
  2. **G3 数据确认** — `validation_queue` 实测 1 条残留污染: status=`verified` 但 verdict=`inconclusive`（validation_id bba12172-9353-48c6-b21c-7d2d6f3bb0dc，2026-07-03 入库，reason="有 1 个来源确认; 包含工具输出特征"）。且该表 CHECK 约束只允许 pending/assigned/validating/verified/refuted/timeout，**不含 inconclusive**——即 G3 不仅是代码 bug，还造成了存量脏数据，修复建议 3 需同步补数据清洗。
  3. **表结构交叉验证** — 该条目引用的 finding_hypotheses / finding_validation_gates DDL 与报告 V5 声明逐字一致（validation_status CHECK 五值、六道 gate CHECK），提升 V5 置信度。
- **边界声明**: 该条目本身是工具使用痕迹（标题即"[任务] ┊ review diff"），未作为独立结论引用；仅取其指向的表路径作复核线索。未做任何写库操作，仅只读查询。
