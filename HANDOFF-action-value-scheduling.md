# HANDOFF — 公司蜂群价值调度优化 (Action-Value Scheduling)

**创建**: 2026-07-18
**范围**: `research/swarm-knowledge/`(公司蜂群运行时)
**状态**: P1 已完成并验证;P2/P3/P4 待办
**目标**: 把 `research/swarm.zip`(ReverseLibrary 支线,已验证的 action-value 调度)的好东西移植进公司蜂群,让它**更强更优化**。

> 新会话必读顺序:本文件 → `src/swarm/action_value.py` → `src/swarm/work_queue.py` → `tests/test_action_value.py`。

---

## 0. 背景:别把两套 swarm 搞混

工作区里有**两套**"蜂群",同源不同码:

1. **`research/swarm.zip`** = ReverseLibrary **支线研究原型**(二进制逆向 Agent,PostgreSQL,IDA)。它的 `reverse_engine/swarm/value.py` / `telemetry.py` 是本次优化的**参考实现**。解压位置(如已删可重新解压):`/tmp/swarm_extract/...`,或 `unzip research/swarm.zip`。
2. **公司蜂群** = 本次要优化的**生产系统**,就在 `research/swarm-knowledge/`(SQLite `swarm_knowledge.db`)+ `company/automation/company_router.py` + `scripts/swarm-orchestrate.py`。

两者都**不是**仿生 PSO/ACO/ABC 蜂群算法。

**优化前的问题**:公司蜂群调度的全部智能 = `src/swarm/work_queue.py:231` 的 `ORDER BY priority DESC`,而 `priority` 是发布时**写死的常数**(`work_queue.py:170-183`:exploit=90/analyze=80/report=65…)。`token_cost` 和 `result_summary` 存了但从不回喂排序 → 没有学习、没有成本感知、没有探索。

---

## 1. P1 已完成 ✅ —— 学习型价值调度(opt-in,可回退)

### 改了什么(4 处)

| 文件 | 内容 |
|---|---|
| `migrations/014_action_value.sql`(新增) | 加 `agent_tasks.base_priority`(保存原始手工优先级)+ `scheduler_decisions` 表(每个候选的 value/features/rank/mode,给 P2 用) |
| `src/swarm/action_value.py`(新增) | 策略本体 |
| `src/swarm/orchestrator.py`(改 2 行) | `_tick_work_market()` 里,"回收 stale claim"之后、`poll_work_tasks` 之前,插了一句 `maybe_rescore_pending(self.db, run_id)`;顶部加 import |
| `tests/test_action_value.py`(新增) | 7 组 pytest |

### 核心机制(`action_value.py`)

价值公式(移植 `value.py` 并适配市场模型,权重是常量便于消融):
```
value = 0.35·P(success)·prior + 0.20·unlock + 0.15·coverage
      + 0.10·novelty + 0.10·prior − 0.10·cost
P(success) = (informative + 1) / (attempts + 2)      # Beta(1,1) 平滑
exploration_bonus = 1 / sqrt(attempts + 1)           # explore 配额用
```
- **`prior`** = 原手工 `priority/100`,降级为特征(没丢弃)。首次重排时把原值存进 `base_priority` 保护。
- **`informative`** 判定 = 完成任务的 `result_summary` 里有 `captured_entry_id`/`finding_id`/非空 `findings|evidence`(见 `worker.py:249-251`)。这是"真产出了知识"的信号,比"非空"强。
- **`unlock`** = 该任务的 pending 子任务数(`parent_task_id` 指向它)/ 3,封顶 1。
- **`novelty`** = 1/(1+本 run 内同指纹已完成数)。
- **`cost`** = 历史平均 token / `COST_REFERENCE_TOKENS`(30k),封顶 1。
- **签名** `signal_fingerprint` = `role|task_type|intent|knowledge_type`,让历史在等价任务间泛化。
- **explore 配额**:`exploration_ratio`(默认 0.2)比例的名额从非头部集里按 `exploration_bonus+novelty` 挑,给 `+0.15` 提分让它们浮上来。
- **写回**:把 `value×100` 写进 `priority` 列 → 现有 `ORDER BY priority DESC` 透明变成价值排序,**claim 热路径一行没动**。

### 三个已验证的安全性质

1. **默认零改动**:run 不设 `scheduler_policy=value` 时,`maybe_rescore_pending` 返回 `None`,不写库不改序。
2. **冷启动安全**:无历史 → `P(success)=0.5` → 得分≈手工优先级(实测 34>32>25 保序),随证据积累才偏离。
3. **学习有效**:喂"analyst 有产出+便宜" vs "exploiter 无产出+烧 3 万 token"后,exploiter 静态 90 > analyst 80,但重排后 analyst 44 > exploiter 16(翻转)。

### 怎么开/关(灰度)

```python
from src.swarm.action_value import set_scheduler_policy
set_scheduler_policy(db, run_id, "value", exploration_ratio=0.2)   # 开
set_scheduler_policy(db, run_id, "priority")                        # 回退
```
只影响该 run;写进 `swarm_runs.config` 的 JSON。

### 怎么跑测试(有坑)

**本机没装 pytest**(`requirements.txt` 声明了 `pytest>=7.0` 但环境没配,`python3 -m venv` 的 ensurepip 也坏了/离线)。P1 是用**等价的独立 harness** 验证的(15/15 通过)。新会话若要跑 pytest:
```bash
cd research/swarm-knowledge
pip install pytest            # 需要联网;或用已配好的 env
python -m pytest tests/test_action_value.py -q
```
若仍无 pytest,可仿照上一轮写 `python3 - <<'PY' ...` 直接调 `action_value` 函数断言。

---

## 2. P2 待办 —— 价值 vs 静态优先级 A/B 度量(建议先做这个)

**为什么**:开了 `value` 之后要能拿出"到底强了多少"的数字,而不是凭感觉。数据已经在 `scheduler_decisions` 里累积。

**参考实现**:`swarm.zip` 的 `reverse_engine/swarm/telemetry.py::comparison_summary`(shadow baseline 对比)+ `PAPER_REVIEW_GUIDE_ZH.md` 第 5 节(公平性约束、主要指标)。

**要做**:
1. 新增 `src/swarm/ab_report.py`(或 `swarmctl.py` 加子命令 `ab-report --run-id`):
   - 关联 `scheduler_decisions`(value_rank / mode / features)与 `agent_tasks`(status / token_cost / result_summary)按 task_id join。
   - 计算:`informative actions / 100k tokens`、`actual reward / 100k tokens`、`accepted/failed 比`、每代 explore 命中率、预测 MAE(需要先定义 actual_reward——见下)。
2. **actual_reward**:P1 只存了 predicted value,没存实际 reward。要补一个 `observe` 步骤(移植 `value.py::observe`):任务完成后按 informative/coverage/cost 算实际 reward,存进 `scheduler_decisions`(加列 `actual_reward REAL`, `prediction_error REAL`)或新表 `scheduler_outcomes`。这是 P2 的主要工作量。
3. **配对实验**:同一 target/预算跑两次(`value` vs `priority`),对比单位成本有效产出。注意 P1 是"同队列重排",不是双策略并行——要真 A/B 得跑两个独立 run(参考 guide 4.4 反事实缺失的警告)。

**验收**:能对一个真实 run 输出一份 markdown 报告,含均值/中位数/效应量,明确区分"实测"和"影子对比"。

---

## 3. P3 待办 —— 可靠性硬化

**为什么**:`company/reports/swarm-status-report-20260716.md` 自己点名:单源自证、无外部健康检查、进程可能已退。

**要做**(独立于 P1/P2,可并行):
1. runner 心跳:`swarm_runner.py` 定期写 `swarm_runs.stats.heartbeat_at`。
2. `/health` 端点:localhost HTTP,外部可独立探活(不依赖 swarm 自身 worker)。
3. 僵尸/stale 恢复:`work_queue.recover_stale_work_claims`(900s)已有雏形,补 worker 级僵尸清理 + runner 崩溃重启(`company_router.py::launch_runner` 已记 `runner_pid`/`runner_restarts`)。
4. systemd service:`ActiveState` + `WatchdogSec` 提供 OS 级存活保证。

**验收**:kill runner 后外部健康检查能立刻发现;stale 任务能被回收重排。

---

## 4. P4 待办 —— 成本/模型路由

**为什么**:便宜模型探索、贵模型确认,省钱且不降质。参考 `scripts/swarm-orchestrate.py` 的 6-phase 成本分层经验 + `src/swarm/model_config.py`。

**要做**:
- 在 `action_value` 选中任务后,按 value/cost 档位选 `model_profile`(高 value+需确认 → 贵模型;探索 → 便宜模型)。
- 钩子点:`work_queue.publish_work_task` 已有 `model_profile_id`;或在 worker claim 后按 decision.mode(explore/exploit)选 profile。

**验收**:同等有效产出下总 token 成本下降,且 explore 任务走便宜模型。

---

## 5. 安全约束(所有后续工作都遵守)

- `swarm_knowledge.db` 是**线上库**(状态报告显示有 run 在跑)。改 schema 用新 migration 文件(`015_*.sql`…),幂等(`db.init()` 靠 `IF NOT EXISTS`/捕获 `already exists`/`duplicate column`)。
- **保持默认 `priority` 策略零改动**:任何新特性都走 opt-in flag,别动 `poll_work_tasks` 的热路径。
- 别把 `swarm.zip` 支线代码直接拷进公司仓库(不同 DB/依赖,且 swarm.zip 无 LICENSE)。只移植**思路**。
- 公司蜂群的外部动作(发布/提交 HackerOne/付款/删除)始终需人工审批——见 `company/.hermes.md`。

---

## 6. 文件锚点速查

| 作用 | 位置 |
|---|---|
| 价值策略 | `src/swarm/action_value.py` |
| 静态排队(被价值写回接管) | `src/swarm/work_queue.py:212` `poll_work_tasks` / `:231` ORDER BY |
| 重排接入点 | `src/swarm/orchestrator.py` `_tick_work_market()` |
| 数据模型 | `migrations/014_action_value.sql`;基表 `001_schema.sql`(agent_tasks/swarm_runs)、`006_work_market.sql`(priority/signal_key…) |
| 完成/informative 信号 | `src/swarm/worker.py:249-283` |
| runner 主循环 | `src/swarm/runner.py::run_until_idle` / 顶层 `swarm_runner.py` |
| 参考实现(支线) | `swarm.zip` → `reverse_engine/swarm/value.py`(公式)、`telemetry.py`(A/B)、`generation.py`(观察/收敛)、`PAPER_REVIEW_GUIDE_ZH.md`(实验设计) |

---

## 7. 一句话状态

P1(价值调度)已上线可灰度、可回退、有测试;**建议新会话从 P2 起步**——补 `actual_reward`/`observe` 并写 A/B 报告,先把"变强了多少"量化出来,再推进 P3/P4。
