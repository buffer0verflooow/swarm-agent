# 蜂群角色缺失分析报告

> 分析日期: 2026-07-25
> 分析师: swarm-worker (analyst) + Codex (gpt-5.6-sol)
> 分析源: controller-worker-architecture.md, orchestrator.py, controller.py, run_manager.py, swarm-architecture.html

---

## 一、当前角色全景

### Worker 层 (4 种)

| 角色 | 职责 | 代码锚点 |
|------|------|----------|
| Scanner | 表面测绘、服务指纹、JS提取、内容发现 | `build_seed_tasks()` in run_manager.py |
| Analyst | 信号分类、假设验证、深度分析 | 同上 |
| Exploiter | 漏洞利用验证 | 同上 |
| Reporter | 滚动报告维护 | 同上 |

### 元层 (6 个组件)

| 组件 | 机制 | 周期 | 职责 |
|------|------|------|------|
| Orchestrator | 规则驱动 | 多级(2s-60s) | 主循环: spawn/心跳/治理/power schedule |
| Controller | LLM 驱动 | 60s | kill/boost/spawn/redirect 决策 |
| Governance Engine | 规则+聚类 | 60s | DIKW 提升、信息素衰减、策略蒸馏 |
| Stigmergy Signal | SQLite | 持续 | 间接协调总线 |
| Capture | 规则 | 事件触发 | 知识条目写入+信号发射 |
| P5 Verification | 3层LLM | 报告时 | 事实/上下文/价值审查 |

---

## 二、缺失角色评估

### 2.1 蜂后 (Queen) — ⚠️ 部分覆盖

**当前覆盖**: Controller 的 kill/boost/spawn/redirect 决策机制已经承担了 Queen 的"调兵/生命周期控制"部分。Controller 已有:
- Worker 信号收集 (quality/novelty/efficiency/loop detection)
- 全局 budget 感知 (spent/total/ratio)
- 决策写入 spawn_requests

**差距**:
1. **无战略层** — Controller 只能做战术决策（杀/加/生/转），不能做战略决策（"这个 target 打完了换另一个"、"从 web 转 API"）
2. **无跨任务仲裁** — 当多个 run 竞争资源时，谁优先？
3. **无失败接管** — Controller 自己出错了（LLM 不可用→降级到 rules 模式），没有更上层的兜底
4. **无历史学习** — 决策只基于当前窗口 (~120s) 的信号，不参考过去 run 的成败模式

**结论**: Controller = 战术执行器，不是战略决策者。Queen 缺失的核心不是"谁下命令"，而是"谁有跨 run 视野做战略转向"。

### 2.2 先知 (Prophet) — ❌ 完全缺失

**确认缺失**: 蜂群中没有任何角色承担以下职责:
- 跨多 run 的综合模式发现
- "基于已发现内容，预测哪些路径最值得挖"
- L3 Knowledge → L4 Wisdom 的提炼

**证据**:
- `L4 Wisdom = 0` 条目 — 架构文档明确标注 `(0)` 且虚线边框
- 没有 `prophet.py` 或类似文件
- Controller prompt 模板中不包含"历史模式回顾"部分
- Cross-run 学习不存在: 每个 `start_swarm.py --intent recon` 从零开始

**必需的闭环**: `历史结果 → 提炼先验 → 生成下一轮假设与探索方向 → 结果回灌`

### 2.3 困境指引者 (Guide/Rescuer) — ❌ 完全缺失

**确认缺失**: 蜂群没有独立于 Controller 的逃生机制。

**当前困境路径**:
```
Worker loop_detected → Controller 60s后检测到 → kill + spawn 同类Worker
                                                        ↓
                                                  同类Worker 可能重复同样的低效路径
```

**问题**:
1. **Controller 自循环** — Controller 自己做决策，自己评估效果，没有独立检查
2. **无回滚机制** — "回滚到最后有效前沿"不存在
3. **无异质策略注入** — kill 后 spawn 同类 scanner 是用同一套方法论继续撞墙
4. **无超时安全停止** — 没有"超过 X 小时无新发现 → 停止 run 并归档"

**对比成熟系统**: Kubernetes 的 CrashLoopBackOff 有指数退避、有 backoff limit 最终停止。当前蜂群没有类似机制。

---

## 三、Codex 关键评估摘录

> "Controller 只是战术执行器"

> "L4 Wisdom = 0 意味着系统会重复试错，长期能力不增长"

> "若只能补一个，优先补独立于 Controller 的 Guide（可兼具 Prophet 能力）"

---

## 四、优先级建议

| 优先级 | 缺失角色 | 理由 |
|--------|----------|------|
| P0 | **Guide/Rescuer** | 群体死锁是最严重的运行时故障，无逃生路径意味着 run 只能硬等超时 |
| P1 | **Queen (战略升级)** | 在 Controller 基础上增加战略层: 跨 run 视野、目标优先级、转向决策 |
| P2 | **Prophet** | 依赖 L4 Wisdom 积累，先有数据才能预测 |

### P0 快速实施方案

```python
# guidance/supervisor.py — 独立于 Controller 的困境检测+逃生
#
# 独立循环 (非 Orchestrator tick 之一):
# - 每 120s 检查所有 run 的全局健康度
# - 检测条件: (所有 worker loop_detected) OR (N 小时无新 L2+ 知识) OR (budget 耗尽无 HIGH 发现)
# - 逃生动作: 回滚→注入异质seed→escalate to human→归档run
# - 与 Controller 的关系: Guide 可以 override Controller 的决策 (override_decision 表)
```

---

## 五、总结

```
当前: Scanner → Analyst → Exploiter → Reporter
                                        ↓
                                     P5 验证
                    ↑ Controller 做战术微调 (60s tick)
                    ↑ Governance 做知识沉积 (60s tick)

缺失: [Queen] 战略转向层 — 谁决定"不挖了"
      [Prophet] 跨 run 学习层 — 谁从失败中提炼 Wisdom
      [Guide] 困境逃生层 — 全群死锁时的独立救生筏
```

最重要的结论: **蜂群不缺执行角色，缺的是"在错误方向上能自动停下来的机制"**。这需要 Guide/Rescuer。
