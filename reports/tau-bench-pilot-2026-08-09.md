# τ-bench 蜂群 Pilot：通用多轮客服任务（2026-08-09）

> Stanford τ²-bench（airline 域）。蜂群架构首次应用于**非安全**领域——
> 多轮客服对话 + 工具调用 + policy 合规。验证"通用自治蜂群"的跨域泛化。

## 实验设计

- 域：airline（50 任务，多轮客服：取消/改签/退款/身份验证）
- 单 agent：官方 LLMAgent（deepseek-chat，litellm 接入）
- 蜂群：**SwarmPolicyAgent（PolicyGuard）**——每轮主 LLM 生成候选 →
  独立 policy 验证器 LLM 审查（对照 domain policy）→ 违规则修正轮
  （violation + guidance 反馈重新生成，最多 2 轮）
- 验证：τ-bench 官方 Pass^1（任务目标达成率）+ DB Match（动作正确率）
- 模型：deepseek-chat（官方 API，thinking disabled 已生效）

## 结果（2 轮 × 10 任务 = 20 次/模式）

| 轮次 | 单 agent Pass^1 | 蜂群 Pass^1 | 蜂群 DB Match |
|---|---|---|---|
| 轮1 (10 任务) | 0.800 (8/10) | **0.900 (9/10)** | **100% (10/10)** |
| 轮2 (10 任务) | 0.800 (8/10) | **0.900 (9/10)** | **100% (10/10)** |
| **合计** | **16/20 (0.800)** | **18/20 (0.900)** | 20/20 |

**蜂群稳定 +10pp，两轮完全复现**。DB Match 100%（单 agent 80-90%）
——蜂群的动作序列（工具调用）更准确。

## 蜂群增值机制（任务 1 解剖）

任务 1（身份/所有权混淆 + 退款 policy）：用户要求取消预订并退款，
但 policy 规定 24h 后不可退。单 agent 失败（2/2 轮）；蜂群成功路径：
检查资格 → 判定不可退 → **transfer_to_human_agents 转人工**（policy 合规动作）
→ reward=1.0。

验证器每轮显式审查 policy（不依赖主 agent 自觉），捕获"取消+退款"
这类需要 policy 判定的动作。

## 成本

| 模式 | 每对话成本 | 说明 |
|---|---|---|
| 单 agent | $0.0010 | 每轮 1 次 LLM 调用 |
| 蜂群 | $0.0018 | 每轮 +1 次验证器调用（1.8 倍）|

**+$0.0008/对话 换 +10pp 准确率**——性价比极高。

## 跨域全景（蜂群 vs 单 agent）

| 领域 | 基准 | 单 agent | 蜂群 |
|---|---|---|---|
| 数据库 (MARBLE) | 100 任务 | F1=0.873 | **F1=1.000** |
| 安全 (BountyBench) | 82 次统计 | 54% | **61%** |
| 客服 (τ-bench) | 20 次统计 | 0.800 | **0.900** |

**三个完全独立的领域，蜂群全部 ≥ 单 agent——"通用自治蜂群"跨域实证完成。**

## 工程要点

- τ²-bench: Python 3.13 venv + `audioop-lts` 兼容包（3.13 移除 audioop）
- litellm 接入 deepseek（deepseek/deepseek-chat → 官方 API）
- 代理：HTTPS_PROXY=127.0.0.1:7890（mihomo）
- 坑：MultiToolMessage 分支重复 extend → 孤立 ToolMessage（deepseek
  tool-role 配对校验失败）→ 修复为单次 extend + 直接 generate
- 注册：registry.register_agent_factory(create_swarm_policy_agent, "swarm_policy_agent")

## 产物

- 代码: research/tau2-bench/src/tau2/agent/swarm_policy_agent.py（已 commit）
- 结果: research/tau2-bench/data/simulations/（4 次运行全轨迹）
- 报告: reports/tau-bench-pilot-2026-08-09.md
