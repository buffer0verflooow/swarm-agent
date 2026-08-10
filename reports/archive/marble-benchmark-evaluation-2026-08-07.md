# MARBLE 全量 100 任务批量运行 + Claude Code 独立评估 — 状态报告（v2，合入 f22d81b5）

- 日期：2026-08-07 23:27（本地，v2 更新）
- 运行：company-analyze-6_1a3310（run bbc5fbba）
- 目标：委派 Claude Code 对现有蜂群的 MARBLE benchmark 结果进行评估
- 状态：批量运行进行中（22/100，22/22 exact），**Claude Code 独立评估三连败、产出不存在、待重试**
- 知识来源：analyst-01 高置信输出 [f22d81b5]（L3，15:24:55 入库）+ reporter 本机交叉复验

## 一、已验证证据（✅ 全部经本机复验）

### 1. Claude Code 独立评估 — 三连败，产出不存在 ❌（v2 核心更正）
前一版报告（23:20）称"评估已委派并在后台执行"——**该信息在写报告时已过期**（claude 进程 23:19:39 已死）。f22d81b5 证据链 + reporter 实测复核：

| 时间 | 进程 | 结果（analyst 证据） | reporter 复核 |
|---|---|---|---|
| 23:16:09 | claude --print --betas context-1m --max-turns 50 (PID 2645744) | 23:19:39 exit 1：API Error 429 Service Unavailable | ✅ /proc/PID 已不存在 |
| 23:20:01 | codex exec ... --max-turns 80 (PID 2668268) | 23:20:42 exit 2：unexpected argument '--max-turns' | —（进程已退出，无残留） |
| 23:21:16 | codex exec ... --max-turns 80 (PID 2675417) | 23:22:21 exit 2：同一参数错误 | — |

- 23:23:22 实测 `/tmp/claude-swarm-eval.md` **不存在**；23:27 reporter 复测仍不存在；无 clawgod/codex/claude 进程存活
- 父会话 @session:default/20260807_230906_1a3310 在 23:22:21 第二次 Codex 失败后停止，无后续重试（messages_after=0）
- **失败根因**：① Claude 429 限流（与记忆中的已知故障一致）；② Codex 参数错误——本机 codex 版本 exec 子命令**没有 --max-turns flag**（标准模式是 `--sandbox danger-full-access --skip-git-repo-check`，tip 提示需用 `-- --max-turns` 传值），人为加参导致 CLI 拒绝

### 2. 全量批量运行 — 22/100，健康 ✅
- 进程：PID 2508753（bash 包装）/ 2508853（python），22:52 启动 — reporter 23:27 实测均存活（state=S）
- Checkpoint `benchmarks/marble_batch_results.json`：**22 条（task 0-21），22/22 exact，F1=1.0，零 error**
- 日志 `/tmp/marble_batch_full.log`：187 行，task 21 (FETCH_LARGE_DATA) exact，task 22 (INSERT_LARGE_DATA) 执行中；**0 条 ERROR/Traceback**
- 已覆盖根因（单根因段）：INSERT_LARGE_DATA / LOCK_CONTENTION / VACUUM / REDUNDANT_INDEX / FETCH_LARGE_DATA 全中
- 速率 ~85-111s/任务（log 实测）→ 剩余 ~78 任务 ≈ **1.7-2h**；容器栈 4 容器 up（前版已核验）

### 3. 数据集结构 — reporter 独立解析 database_main.jsonl（100 任务）✅
- **task 0-49 = 全部单根因（50 个），task 50-99 = 全部双根因（50 个）**，恰好 50/50 分界于 task 50
- **实际只有 5 类根因**（各 10 次单根因 + 10 次双根因组合）：INSERT_LARGE_DATA / REDUNDANT_INDEX / LOCK_CONTENTION / VACUUM / FETCH_LARGE_DATA
- MISSING_INDEXES / POOR_JOIN_PERFORMANCE / CPU_CONTENTION **在 ground truth 中从不出现** —— 数据集属性，不是采样偏差
- 双根因组合恰好 5 种 ×10：FETCH+VACUUM / FETCH+INSERT / INSERT+LOCK / **LOCK+REDUNDANT** / REDUNDANT+VACUUM
- **修正 analyst 一处计数**：LOCK_CONTENTION+REDUNDANT_INDEX 组合 = **10 个**（非 analyst 所述 13 个；13 或来自含 LOCK 的双根因合计 20 的误读）。不改变结论——该组合仍是 pilot 已知短板，task 51/52/59 等即属此类
- task 51 = LOCK_CONTENTION + REDUNDANT_INDEX，**未跑到**（现 22/100）

### 4. Runner DECISION RULES 精度偏置 — 源码核验 ✅
`benchmarks/marble_db_runner.py` L205-221（reporter 直接读取）：
- **Rule 2**（L208-211）：REDUNDANT_INDEX verifier 确认 ≥5 个未用索引时，update_table1 调用视为脚本验证负载 → **除非 ≥150k 且场景确实双根因，否则不报 LOCK_CONTENTION**（SECOND-ORDER 抑制）——正是 pilot task 51 漏 LOCK 的机制
- **Rule 6**（L221）："Report 1-2 root causes max. Precision matters more than recall." —— 双根因任务 recall 结构性受压

### 5. Pilot 基线（08-06 报告，本机文件）✅
- 启发式 6/7（F1≈0.95）；单 LLM worker（deepseek-v4-flash）6/7（F1≈0.95）；蜂群 v2 8/8（F1=1.0）
- v1→v2 修复：共享信号快照（tool_calls 40+→2-5）、判据化 verifier、全证据 lead 汇聚
- database 场景为真执行式评估（真实 Postgres+Prometheus），符合 P5 铁律

## 二、不确定性（⚠️）

1. **批量未完成（22/100 = 22%）**——F1=1.0 仅覆盖单根因段，不能外推
2. **单根因段 F1=1.0 不具信息量也不可疑**——每类异常在 pg_stat_statements 有截然不同的查询签名，阈值判据即可分离（启发式基线都 6/7）。这是判据化 verifier 的预期表现，**不是蜂群强弱的证明**
3. **真正的考验在 task 50-99 双根因段**：Rule 2/6 的精度偏置设计预计造成 LOCK 系统性漏报（尤其 LOCK+REDUNDANT 10 个组合）；task 50 预计 **~00:03** 到达
4. **独立评估产出缺失**：三连败后父会话停止，无任何第三方第二意见；重试仍可能撞 Claude 429（冷却时长未知）
5. **无对照模式运行**：heuristic/llm 基线仍是 7 任务小样本（08-06 pilot），规模化对比缺失
6. analyst-01 首轮交接曾被截断（15:16 版），本版基于其终版输出 f22d81b5（15:24:55），无截断

## 三、影响

- **"20/20 F1=1.0"不构成对蜂群架构的强验证**——单根因判别近乎平凡；若双根因段全中，蜂群 > 单 agent（8/8 vs 6/7）才获得统计意义
- **双根因段是成败关键**：Rule 2 SECOND-ORDER 抑制（150k 上限）+ Rule 6 的 1-2 上限意味着**精度强、召回弱**是设计的预期形态；若 LOCK+REDUNDANT 系统性漏 LOCK，全量结论须降级为"精度强、召回弱"
- **独立评估缺位 = 架构第二意见缺失**，与多层评估偏好冲突；且 Claude 429 是已知复发性故障，Codex 参数错误是本次人为失误（可修复）
- 结果与 08-06 pilot 一致则 MARBLE database 场景可正式成为蜂群测试集（milestone KPI）

## 四、修复与下一步（Remediation）

1. **重新委派评估（目标未达成，不可算完成）**——正确命令（修复版）：
   - Codex：`codex exec --sandbox danger-full-access --skip-git-repo-check -p "$(cat /tmp/swarm-benchmark-eval-context.md ...)"` —— **去掉 --max-turns**；brief 已存在（/tmp/swarm-benchmark-eval-context.md，4837 字节）
   - 或 Claude：错峰重试（429 冷却后），仍用 `claude --print --betas context-1m-2025-08-07`
   - **委派铁律**：再失败只报告失败，主/worker 严禁自己动手写评估
2. **重点观察窗口**：~00:03 起 task 50-99 双根因段；task 51 是 LOCK+REDUNDANT 首个试金石——若漏 LOCK 复现，调 SECOND-ORDER 阈值（150k 上限）或 Rule 6 的 1-2 上限
3. **等待批量完成**（~1.7-2h）→ 汇总 100 任务精确率 + per-root-cause 表（marble_batch.py 自带汇总输出）
4. 收集重试后的独立评估产出 → 读 `/tmp/claude-swarm-eval.md`（若成功）与本次报告合并
5. **可选**：全量跑 heuristic / llm 模式做同规模基线对照（各 ~2-3h）
6. 全程无需外部探测；所有结论基于本机进程/文件/日志/源码/数据集，无需新授权

## 验证标记

- ✅ = 已核验（本机进程 /proc、checkpoint JSON、日志行、runner 源码行号、数据集全量解析、父会话记录）
- ⚠️ = 进行中/推断（双根因段未到、429 冷却未知、批量未完成）
- ❌ = 已确证失败（委派三连败、评估产出不存在）
