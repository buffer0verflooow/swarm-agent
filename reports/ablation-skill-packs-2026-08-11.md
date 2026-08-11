# 第 1 层修正 ablation 评估报告: 角色→技能包映射 (2026-08-11)

## 背景

三层修正方向(技能包映射 / 任务→角色推导 / 代际接力)源自 2026-08-11 会话,
从 reverselibrary 架构类比而来, 从未做过实证评估。用户质疑"是否真的有效"。
评估前置条件(正确性层 G1/G2/G3 修复 + 生产库清洗)已完成并合入
(318fc12, 8add0da)。

本报告回答: **第 1 层(角色→技能包映射)是否提升蜂群漏洞挖掘成功率?**

## 第 1 层实现 (commit eff106a)

- migrations/008_role_skill_packs.sql: model_profiles 增加 load_skills / tool_allowlist,
  预置 scanner/analyst/exploiter/reporter/custom 五角色技能包(二进制场景方法论)
- model_config._row_to_profile 转发两列(JSON->list)
- worker.build_task_context 按 task.model_profile.load_skills 注入 "## Role Skills" 段
- ablation 开关: SWARM_SKILL_PACKS=0 禁用(基线组), 默认启用
- 测试: tests/test_role_skill_packs.py 4 项, 全量 179 passed

## 实验设计

- 任务: pwn.college readelf-cve-2021-20294 (binutils 2.35 符号版本栈溢出)
- 判定: `readelf -s <file>` signal 崩溃 (P5 执行式, 零幻觉空间)
- 组别: baseline(无技能提示) vs skills(注入 Role Skills), 各 5 轮
- 两档难度:
  - easy: FIX prompt 含构造半答案(f-string 三坑 / .symver 语法 / version-script 机制)
  - hard: 移除半答案, 只留判定条件 + 环境事实(区分度模式)
- 脚本: benchmarks/pwn_ablation_skills.py (复用 pwn_college_runner 判定)

## 结果

| 组 | baseline | skills | 差异 |
|---|---|---|---|
| easy | 5/5 (100%), avg 7.0s, FIX 1-2 次 | 5/5 (100%), avg ~7s, FIX 1 次 | 无 (天花板) |
| hard | 0/5 (0%), avg 63.7s | 0/5 (0%), avg 66.1s | 无 (地板) |

原始数据: benchmarks/pwn_ablation_baseline.json (easy baseline),
pwn_ablation_hard_baseline.json, pwn_ablation_hard_skills.json
(easy skills 5/5 由运行日志见证, JSON 被后续冒烟覆盖)。

## 结论: 第 1 层未通过评估

按预设判定标准(技能包组成功率 > 基线才继续推进第 2/3 层):

**第 1 层在 readelf 挑战上无可测量的效果, 第 2/3 层不应基于当前证据推进。**

- 天花板: easy 档两组均 100% —— 提示本身含半答案时, 技能包无边际贡献
- 地板: hard 档两组均 0% —— 移除半答案后, 方法论型技能包救不了领域知识缺口

## 失败模式分析 (hard 两组的共同瓶颈)

两组失败的根因相同: agent 无法自行发现 ".symver + version-script 超长版本名"
这条触发路径。缺失的是**领域事实**(该漏洞的具体触发构造), 不是工作方法。

技能包内容是"通用方法论"(构造优先 / 失败反馈闭环 / 字节级精度 / 语言意识),
对领域知识缺口无效 —— 方法论提示不能替代漏洞机理知识。

## 对三层修正的总体判断

1. **第 1 层机制有效但内容无效**: 技能注入管线可运行(上下文正常注入),
   但"方法论型"技能包在漏洞挖掘任务上零增益。要有效需要"领域事实库"
   (按 CWE/目标类型注入具体检测与构造规则) —— 那已不是技能提示, 是规则注入,
   属于另一个设计(且与 G2 验证脱钩强相关: 规则库需要先验证规则本身)。
2. **第 2 层(任务→角色推导)**: 编排自动化, 不改变 agent 能力; 第 1 层无效时
   无实施基础。
3. **第 3 层(代际接力)**: 成本最高, 收益未证实; 无评估价值在当前数据下。

## 评估局限

- 单任务(readelf): 其他领域/难度未覆盖
- 单模型(deepseek-v4-flash): 更强模型可能有不同表现
- 技能包内容为方法论型: 领域事实型内容未测
- easy 档提示本身含半答案(评估前置污染); hard 档对当前模型过难(0% 地板)

## 建议

- 若坚持第 1 层, 下一实验应注入"领域事实型"技能包(如: 版本段处理漏洞 →
  尝试 .symver + version-script; 解析器边界 → fuzz 入口) 并在中间难度任务上对照
- 更优先的方向: 把评估预算投向 G2 replay_verifier 真实接线(已在 8add0da 落地
  注入点, 未接真实回放器)与验证脱钩 —— 那是 10% 准确率问题的直接对症修复
- 三层修正的结论固化: 未获得实证支持, 后续任何实现须先过 ablation 关
