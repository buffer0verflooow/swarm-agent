# Third-Party Skill Attribution — Cybermes (2026-08-24 迁移)

以下技能迁移自开源项目 [Zyrexnn/Cybermes](https://github.com/Zyrexnn/Cybermes)
(仓库许可证: Apache-2.0, 商业使用免费)。迁移保留原 SKILL.md 内容与结构。

## 迁移范围

- recon/ 新增 15 个技能: api-recon-and-docs, fuzzing-and-content-discovery,
  graphql-and-hidden-parameters, http-parameter-pollution, hunt-fintech-graphql,
  insecure-source-code-management, js-recon-secret-hunting, oast-blind-testing,
  parameter-mining, recon-and-methodology, recon-for-sec, recon-scope-triage,
  recon-fallbacks, stealth-web-recon, source-code-audit, subdomain-takeover
- redteam/ 新增 ~113 个技能: 覆盖 AI/ML 安全 (ai-ml-security, custom-ai-router-assessment,
  ai-api-gateway-security, new-api-pentest, hunt-rag-vector)、Web 深潜 (race-condition,
  request-smuggling, prototype-pollution-advanced, waf-bypass-techniques 等)、
  移动/云/AD (android-pentesting-tricks, ios-pentesting-tricks, active-directory-*,
  ntlm-relay-coercion, kubernetes-pentesting)、二进制/exploit (heap-exploitation,
  kernel-exploitation, stack-overflow-and-rop, browser-exploitation-v8 等)、
  密码学 (rsa-attack-techniques, lattice-crypto-attacks 等)

## 上游说明

Cybermes 自身的技能内容多源自公开 bug bounty 报告与社区方法论 (hunt-* 系列声明
"Built from N public bug bounty reports")。若技能内文有独立上游引用，以各技能内
ATTRIBUTION/来源声明为准。

## 未迁移

- knowledge/ payload 数据库 (186MB): 含 HackTricks (CC BY-NC 4.0, 非商用) —
  整体搬入有许可风险，故不迁移
- strix-* 集成技能 (4 个): 绑定 Strix 框架，与蜂群架构无关
- 与既有技能重复的同源 hunt-*/bb-* 技能 (~110 个)

迁移执行: Hermes Agent, 2026-08-24。原始源码快照: /tmp/cybermes/Cybermes-main
