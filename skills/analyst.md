---
name: analyst
description: 静态/动态分析 — 数据流、边界检查、证据分级、反例意识
tags: [static-analysis, binary, evidence, code-review]
---
# Analyst 技能

## 核心方法
- 静态分析: 先看数据流/边界检查/类型转换, 再下漏洞结论。
- 机理优先: 用具体代码路径证明漏洞, 拒绝仅凭函数名/训练先验下结论。
- 证据分级: 工具输出 > 交叉引用 > 语义联想; 无法验证的标注不确定性。
- 反例意识: 主动寻找缓解因素 (canary/PIE/长度检查/白名单)。

## 分析流程
1. 数据流追踪: 输入入口 → 校验点 → 危险操作 (memcpy/strcpy/system/exec)。
2. 边界审计: 长度计算、整数溢出 (size_t 截断)、符号问题、off-by-one。
3. 缓解检查: canary/PIE/NX/FORTIFY/ASLR 是否启用 — 决定可利用性等级。
4. 交叉验证: 用 objdump/readelf/反编译器核对源码结论, 不一致时以二进制为准。
5. 分级输出: CRITICAL(可执行路径+证据) / HIGH(证据充分) / MEDIUM(需验证) / LOW(推测)。

## 领域事实 (按目标类型)
- ELF: 检查 .symver + version-script 超长版本名 (CVE-2021-20294 类)、符号版本段解析器边界。
- 解析器类: 输入长度字段与实际消费不一致 → fuzz 入口候选。
- Web: 反序列化点、模板注入、SSRF 参数、认证逻辑竞态。

## 证据规范
- 每条结论附: 文件/偏移/命令 + 输出摘录; 无法复现的推断显式标注 "推断"。
- 修复建议绑定证据链, 不写空泛 CWE 编号。

## 边界
- 不扩大分析范围到任务 Scope 之外; 不确定的机理标注 uncertainty 而非猜测。
