# τ-bench 蜂群 Agent（备份）

τ-bench（tau2-bench）自定义蜂群 agent，源在 research/tau2-bench/src/tau2/agent/。

- `swarm_policy_agent.py` — v1 PolicyGuard（推荐）：每轮主 LLM 生成候选 →
  独立 policy 验证器审查 → 违规则修正轮。airline 4 轮均值 0.875 vs 单 agent 0.800。
- `swarm_intent_agent.py` — v2/v3 IntentTracker（实验）：跨轮诉求清单 +
  hard policy。均值 0.800，无增值（简单 > 复杂结论的实验对象）。

安装：在 tau2-bench 的 registry.py 注册 factory（create_swarm_policy_agent
→ "swarm_policy_agent"），`tau2 run --agent swarm_policy_agent` 调用。

完整报告: reports/tau-bench-pilot-2026-08-09.md
         reports/tau-bench-verifier-optimization-2026-08-09.md
