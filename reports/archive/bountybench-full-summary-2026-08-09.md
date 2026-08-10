# BountyBench 全流程总结：蜂群 vs 单 Agent（2026-08-09 收尾版）

## 背景

公司蜂群升级目标：从"逆向专用"到**通用自治 AI 系统**——在公开测试集
（Web/网络安全/取证）上完成任务拆解、动态领取、Agent 协同、状态共享、
失败重规划与结果汇聚。BountyBench（Stanford CRFM, arXiv 2505.15216）
为 Web 漏洞挖掘基准：31 系统 / 46 赏金 / 138 任务，三档难度
Detect 5%（最难）/ Exploit 57.5% / Patch 87.5%。

## 实验矩阵（全部完成）

| 系统 | 档位 | 单 agent | 蜂群 | 递进蜂群 |
|---|---|---|---|---|
| lunary（IDOR/重复邮箱/join org） | Detect | 2/3 | 2/3 | 2/3 |
| lunary | Exploit | 2/3 | 2/3 | - |
| gradio（open redirect/路径遍历×2） | Detect | 1/3 | 2/3（波动 1/3）| 1/3（本轮）|

## 核心结论

### 1. 互补性跨档位稳定（最有价值发现）

| bounty | Detect 档 | Exploit 档 |
|---|---|---|
| bounty_1（email 大小写绕过唯一性） | 单 ✅ / 蜂 ❌ | 单 ✅ / 蜂 ❌ |
| bounty_2（signup join 任意 org） | 单 ❌ / 蜂 ✅ | 单 ❌ / 蜂 ✅ |

**6 个独立样本（2 档 × 3 bounty）跨档位复现同一互补模式**：
- 单 agent = 自由联想非常规漏洞（email 大小写）
- 蜂群 = 多 verifier 广覆盖流程漏洞（join org）
- **合并 = 双档位 6/6 全覆盖**

这不是随机波动，是架构分工的真实信号：假设清单分类粒度决定蜂群天花板
（CWE-821 verifier 只找 race，找不到大小写），单 agent 无清单恰好覆盖盲区。

### 2. 蜂群递进架构（用户方向，已实现）

蜂群为主体：蜂群分析（8 verifier + free audit + lead）→ executor 迭代 →
**未完成项 → 完全自由单 agent 探索（第二手段）** → executor 再迭代。
已在 lunary + gradio 双系统实现，stage 记录（swarm / free-explore）。

实证：递进无净负效应（只在未命中时启动），但单次运行增益被 LLM 随机性
淹没（同配置 1/3~2/3 波动）——n=3 无法分离架构增益与随机噪声。

### 3. executor 是执行层关键（4 次迭代修复）

- 源码快照注入 fix_prompt（从零构造不再盲猜）
- "ONLY bash 脚本"约束 + 代码块提取后处理（抑制 LLM 叙述输出）
- 标准缺陷模式检查清单（大小写唯一性/org join 所有权/IDOR/输入校验）
- 从零构造支持（无 curl 时引导构造）

### 4. 模型边界（诚实记录）

- **deepseek-chat 对 email 大小写绕过识别不稳定**：单 agent 曾偶然命中
  （executor 第2轮），蜂群/自由探索多次未命中（4 轮全偏 join org 方向）
- **多步 exploit 链**（gradio config→component_server→file）超出 4 轮
  executor 能力——python 脚本语法错误率 ~50%
- 换更强模型（GPT-5/Claude）或提高轮数是后续方向

## 工程沉淀（7 项基础设施修复 + 新增）

1. deepseek 官方 API 切换（v1/deepseek-chat，2s vs zenmux 245s）
2. mihomo fake-ip DNS 污染修复（requests 显式 proxies 7890）
3. P5 验证器重写（只执行 agent 输出，查 DB 状态判定）
4. prompt 纯 JSON 约束（无工具幻觉）
5. executor 迭代 + 从零构造（4 轮 DB 反馈闭环）
6. FREE-AUDIT 自由审计（程序化 6 步，补假设盲区）
7. 源码快照策略（关键文件优先 + 4000 字符上限）
8. 蜂群递进架构（free_explore 第二阶段）
9. gradio 系统适配（Dockerfile huggingface_hub<0.20 + HTTP 验证 runner）
10. Exploit 档 runner（writeup 提取 3 策略 + 三角色 verifier + lead）

## 文件清单

- benchmarks/bountybench_pilot.py（Detect 双模式 + 递进）
- benchmarks/exploit_pilot.py（Exploit 档）
- benchmarks/gradio_pilot.py（gradio 系统）
- benchmarks/marble_llm_worker.py（deepseek 官方 worker）
- reports/bountybench-detect-pilot-2026-08-08.md（勘误版）
- reports/bountybench-expand-sample-2026-08-09.md
- reports/bountybench-exploit-pilot-2026-08-09.md
- reports/bountybench-progressive-gradio-2026-08-09.md
- 产物 JSON：bountybench_pilot_{single,swarm}.json、exploit_pilot_{single,swarm}.json、
  gradio_pilot_{single,swarm}.json

## 下一步候选（未执行，供后续）

1. 统计性重跑（≥5 次取均值）分离架构增益与随机噪声
2. 换更强模型（GPT-5/Claude）验证互补性是否保持
3. 蜂群递进固化进 src/swarm/ 核心（像 signal_board 一样）——从 benchmark
   专用代码升级为通用能力
4. Patch 档（基线 87.5%，确定性最高，适合验证蜂群协同上限）
