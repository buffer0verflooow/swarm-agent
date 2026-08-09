# τ-bench 验证器优化实验：简单 > 复杂（2026-08-09）

> airline 域 9 轮 × 10 任务 = 90 次模拟。三版蜂群验证器 vs 单 agent
> 统计对比，验证"验证器设计复杂度"与"效果"的关系。

## 实验设计

三版验证器（都在 τ-bench 官方环境，deepseek-chat）：

| 版本 | 验证器逻辑 | 每轮成本 |
|---|---|---|
| v1 PolicyGuard | 只查 **policy 合规**（候选是否违反 domain policy）| +1 LLM |
| v2 IntentTracker | **跨轮诉求清单**（提取未满足的用户请求 + 检查候选遗漏）| +1 LLM |
| v3 | v2 + **HARD POLICY 轴**（用户要求的违规动作也必须拒绝）| +1 LLM |
| v3.1 | v3 + 循环保护 + 替代方案强化 | +1 LLM |

## 结果（Pass^1）

| 模式 | 各轮成绩 | 均值 | 样本 |
|---|---|---|---|
| 单 agent | 0.800, 0.800 | **0.800** | 20 |
| v1 PolicyGuard | 0.900×3, 0.800 | **0.875** | 40 |
| v2/v3/v3.1 IntentTracker | 0.900, 0.900, 0.700, 0.800, 0.700 | **0.800** | 50 |

## 结论

1. **v1（简单 policy 验证器）是唯一有效的**：+7.5pp（0.875 vs 0.800），
   4 轮稳定。policy 审查是 airline 域的真实增值点。
2. **IntentTracker 系列无增值**（0.800 = 单 agent）：deepseek-chat 的
   上下文记忆已够用，显式诉求清单反而引入干扰（修正轮打断流畅决策）。
3. **复杂度是负优化**：v3 hard policy（用户要求也拒绝 → 死循环风险）
   和 v3.1 循环保护（0.700）都降低成绩。验证器越"全能"越差。
4. **单次运行不可信**：失败任务在版本间漂移（v1:{7}, v2:{1}, v3:{6},
   v3.1:{1,7,8}）——统计重跑是唯一可靠判定方式（架构盲区第 5 层再证）。

## 与 BountyBench 的呼应

- BountyBench: "简单指令约束 > 复杂 prompt"
- τ-bench: "简单单维度验证器 > 复杂多维度验证器"
- 共同规律：**蜂群分工的价值在"单一职责的冗余检查"，不在"全知全能的总管"**

## 工程要点

- 死循环案例：v3 hard policy 强制 agent"拒绝/转人工"，用户拒绝转人工
  → 202 条消息无限重复（τ-bench 撞 max 轮终止）。教训：验证器修正
  指令必须保留主 agent 的对话灵活性。
- 循环保护（候选相同即停）反而恶化（0.700）——修正轮不是瓶颈。

## 最终推荐

**v1 SwarmPolicyAgent（PolicyGuard）为 τ-bench 蜂群标准配置**：
0.875 均值、4 轮稳定、成本 +$0.0008/对话（+80% 换 +7.5pp）。

## 产物

- 代码: tau2-bench/src/tau2/agent/swarm_policy_agent.py (v1, 已 commit)
        tau2-bench/src/tau2/agent/swarm_intent_agent.py (v2/v3, 已 commit)
- 数据: tau2-bench/data/simulations/（9 轮全轨迹）
- 报告: reports/tau-bench-verifier-optimization-2026-08-09.md
