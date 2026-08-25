---
name: secret-scan
description: 凭据狩猎 — 48 模式密钥扫描 (含 AI 时代 API key), 适用 APK/JS/源码审计
tags: [secrets, credentials, apk, js, source-audit]
---
# Secret Scan 技能 — 48 模式凭据狩猎

## 何时使用
- APK 反编译产物 / JS bundle / 前端源码中找硬编码凭据。
- 源码审计中检测泄漏的 API key、私钥、token。
- 供应链/代码仓库扫描 (git history 泄漏的凭据)。

## 用法
```bash
# 目录扫描 (跳过 .git/node_modules/二进制)
python3 scripts/secret_scan.py <dir> [--workers 8]

# stdin 扫描 (管道)
cat bundle.js | python3 scripts/secret_scan.py -

# JSON 输出 (供下游处理)
python3 scripts/secret_scan.py <dir> --json
```

## 覆盖模式 (48 类)
- 云: AWS (AKIA/ASIA), GCP service_account/API key, Cloudflare, DigitalOcean
- AI 时代: Anthropic (sk-ant-api03), OpenAI (sk-proj/sk-legacy/sess-), HuggingFace (hf_)
- 代码托管: GitHub PAT (ghp_/github_pat_/gho_/ghu_/ghs_)
- 支付/消息: Stripe (sk_live/sk_test), Slack (xoxb/xoxa), Twilio, SendGrid, Mailgun
- 包注册表: npm_, pypi-AgENdGV, dckr_pat_
- 私钥: RSA/EC/OpenSSH/GENERIC PRIVATE KEY
- 通用: JWT, Bearer auth, BASIC auth URL, GENERIC_API_KEY
- 机器人: Discord bot, Telegram bot

## 铁律
1. **发现即证据, 但必须验证可用性** — 找到的 key 不等于有效 key; 标记为
   "candidate" 并注明来源文件/行号, 禁止直接声称"可用的生产密钥"。
2. **脱敏** — 汇报中密钥必须截断/脱敏 (只留前缀), 全文只进项目隔离区。
3. **严重级判断** — critical (私钥/AWS/Stripe live/Anthropic/OpenAI) 优先上报;
   low (STRIPE_TEST/FIREBASE_URL/SENTRY_DSN) 归拢信息类。
4. **别污染知识库** — 原始密钥内容禁止 capture 入库, 只入 pattern/severity/
   来源位置元数据。

## 验证
- JSON 输出含 pattern/severity/category/source/line 字段。
- 对每条 critical/high 手工复核正则命中的上下文 (防误报)。
