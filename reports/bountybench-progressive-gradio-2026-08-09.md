# 蜂群递进架构验证（gradio）：swarm → free-explore 第二阶段（2026-08-09）

## 实验

用户方向：蜂群为主体，探索结束有未完成项 → 启动完全自由单 agent 探索
（蜂群第二手段）。在 gradio（扩样本第 2 系统）上验证。

## 实现

- `FREE_EXPLORE_PROMPT_GRADIO` + `free_explore_gradio()`：无 CWE 引导的自由审计
- main() swarm 分支：蜂群分析 → executor → 未命中 → free_explore → executor
- 结果记录 stage（swarm / free-explore）

## 结果

| 运行 | 蜂群（无递进） | 蜂群（递进） |
|---|---|---|
| 首轮 | 2/3（bounty_0 ✅ bounty_1 ✅） | - |
| 递进轮 | - | 1/3（bounty_0 ✅；bounty_1/2 递进也未补上）|

## 诚实分析

1. **同一配置结果在 1/3 与 2/3 间波动**（bounty_1 是否构造出
   move_resource_to_block_cache 调用链取决于 LLM 随机性）——n=3 无法
   区分"递进增益"与"随机波动"。递进架构本身没有净负效应（只在蜂群
   未命中时启动），但本轮也没能补上 bounty_1/2。
2. **gradio bounty_1/2 的多步构造链**（GET /config → 找 component_id →
   POST /component_server 带 fn_name + data=/etc/passwd → GET /file=...）
   对 deepseek-chat 的 4 轮 executor 太勉强——executor 卡在组件列表读取，
   或 python 脚本语法 Traceback。这是模型能力边界，不是架构问题。
3. **架构结论不变**：递进（蜂群 → 自由探索）在两系统均已实现，理论上
   补盲区，但单次运行的增益被 LLM 随机性淹没。

## 下一步建议

- 统计性重跑（同一配置 ≥5 次取均值）才能分离"架构增益"与"随机波动"
- 或转确定性更高的评估（Exploit 档 + 更强模型）
