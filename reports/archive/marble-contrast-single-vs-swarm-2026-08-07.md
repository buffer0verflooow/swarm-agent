# MARBLE 100 任务对照实验：蜂群 vs 单 Agent（2026-08-07）

## 一、实验设计

- 任务集：MARBLE multiagentbench database 全部 100 任务（同任务集）
- 模型：均为 deepseek-v4-flash（zenmux），temperature 0.2（同模型）
- 信号源：同一 PostgreSQL 监控栈 + pg_stat_statements（同信号源）
- 单 agent：全局上下文 + query_db 工具调用 + SECOND-ORDER 规则
- 蜂群：probe 共享信号板快照 → 8 并行 verifier（判据化）→ lead 全证据仲裁
- 结果文件：
  - 蜂群 `benchmarks/marble_batch_results_v2.json`
  - 单 agent `benchmarks/marble_llm_results.json`
- 报告：`reports/marble-contrast-single-vs-swarm-2026-08-07.md`

可复现命令：

```bash
# 蜂群（共享信号板）
.venv/bin/python -u -m benchmarks.marble_batch --mode swarm --start 0 --end 100 \
    --db /tmp/marble_batch_full.db --checkpoint benchmarks/marble_batch_results_v2.json

# 单 agent
.venv/bin/python -u -m benchmarks.marble_batch --mode llm --start 0 --end 100 \
    --db /tmp/marble_batch_llm.db --checkpoint benchmarks/marble_llm_results.json
```

## 二、总体结果

| 指标 | 蜂群 | 单 agent | 差异 |
|---|---|---|---|
| exact | **100/100** | 70/100 | **+30** |
| avg F1 | **1.000** | 0.873 | +0.127 |
| 单根因（50 任务） | 50/50 | 50/50 | 持平 |
| 双根因（50 任务） | **50/50** | **20/50** | +30 |
| 平均耗时/任务 | ~76s（v1） | 63.5s | 单 agent 略快 |
| 平均工具调用/任务 | 2-5 次 | **13.7 次** | 蜂群省 ~75% |
| 总耗时（100 任务） | 217 min | 200 min | 相近 |

**单 agent 失败的 30 个任务全部是双根因**（单根因 50/50 全对）。
**蜂群对而单 agent 错的：30 个；单 agent 对而蜂群错的：0 个。**

## 三、按根因

| 根因 | 蜂群 | 单 agent | 差距 |
|---|---|---|---|
| INSERT_LARGE_DATA | 30/30 (100%) | 29/30 (97%) | +1 |
| FETCH_LARGE_DATA | 30/30 (100%) | 25/30 (83%) | +5 |
| LOCK_CONTENTION | 30/30 (100%) | **11/30 (37%)** | **+19** |
| REDUNDANT_INDEX | 30/30 (100%) | 25/30 (83%) | +5 |
| VACUUM | 30/30 (100%) | 26/30 (87%) | +4 |

LOCK_CONTENTION 是单 agent 最大短板——**37% 检出率，每 3 个漏 2 个**。

## 四、单 agent 失败模式：双根因系统性漏报

失败组合分布（30 个全为双根因）：

| 组合 | 数量 | 单 agent 行为 |
|---|---|---|
| LOCK_CONTENTION + REDUNDANT_INDEX | 10 | 只报 REDUNDANT（漏 LOCK） |
| INSERT_LARGE_DATA + LOCK_CONTENTION | 10 | 只报 INSERT（漏 LOCK） |
| REDUNDANT_INDEX + VACUUM | 5 | 只报 VACUUM 或空诊断 |
| INSERT_LARGE_DATA + FETCH_LARGE_DATA | 3 | 只报 INSERT（漏 FETCH） |
| VACUUM + FETCH_LARGE_DATA | 2 | 只报 VACUUM（漏 FETCH） |

漏报根因分布：**LOCK_CONTENTION 19 次**、REDUNDANT 5、FETCH 5、VACUUM 4、INSERT 1。

完整失败任务清单（30 个）：task 51, 52, 54, 55, 57, 58, 59, 60, 61, 62,
63, 64, 65, 67, 68, 69, 70, 71, 75, 76, 77, 81, 84, 85, 86, 89, 92, 95,
96, 97（均为双根因任务，pred 只报 1 个根因或空）。

## 五、根因分析：单 agent 为什么系统性漏 LOCK

**案例 task 52**（LOCK+REDUNDANT，单 agent 只报 REDUNDANT）：
其 evidence 明确记录 `"write_amplification": "89,901 UPDATEs on table1"`——
**它看到了 LOCK 的核心信号（9 万次并发 UPDATE），却把它归因为 REDUNDANT
脚本的"验证负载"而排除**（SECOND-ORDER 规则：REDUNDANT 场景的 UPDATE
是验证不算 LOCK）。

**机制解释**：单 agent 在单一上下文里必须做"这 9 万次 UPDATE 是验证负载
还是独立根因"的**非此即彼取舍**。SECOND-ORDER 规则（防误报 setup）与
LOCK 检出（要报 UPDATE 洪峰）天然冲突，单上下文无法同时满足——规则偏向
防误报时漏报 LOCK；规则偏向检出时又会误报（这正是蜂群 v1 在 task 2 的
误报来源）。

蜂群则不存在这个冲突：LOCK verifier 独立验证 UPDATE 洪峰（不被 REDUNDANT
上下文污染），REDUNDANT verifier 独立验证索引冗余，lead 仲裁看到两者
证据齐全 → 双双报出。**分工让"互斥判别"不再互斥。**

## 六、附带对比：成本与证据质量

| 维度 | 单 agent | 蜂群 |
|---|---|---|
| 工具调用/任务 | 13.7 次（重复扫描 pg_stat_statements） | 2-5 次（probe 一次采集 + verifier 快照判定） |
| 证据链 | 只有最终摘要 | 每 verifier 完整证据持久化（graph metadata.signal_board，可审计回放） |
| worker 故障容错 | API 失败 = 整任务失败 | verifier 失败由 lead 从共享快照补报（task 97 实证） |

## 七、结论

1. **蜂群相对单 agent 的提升是真实的、可量化的**：同条件（同模型/同任务/
   同信号源）下 100 vs 70 exact，蜂群对单 agent 错的 30 个任务中，单 agent
   全部在双根因上失败。
2. **提升的本质不是"更聪明"而是"更不冲突"**：单上下文的非此即彼取舍
   （防误报 vs 检出）在双根因场景必然损失一边；蜂群用独立 verifier +
   全证据仲裁把冲突消解为并行验证。
3. **单 agent 的 LOCK_CONTENTION 检出（37%）是最大短板**——不是模型
   看不到信号（evidence 里有 9 万次 UPDATE），而是规则冲突导致放弃报告。
4. **效率优势**：蜂群工具调用 2-5 次 vs 单 agent 13.7 次（省 ~75% token），
   证据链完整可审计，对 worker 故障天然容错。
5. **架构启示**：对"多标签/多根因"任务，蜂群信号板模式（共享快照 +
   独立验证 + 汇聚仲裁）是结构性优势；单 agent 只能靠调 prompt 权衡，
   无法根除取舍冲突。
