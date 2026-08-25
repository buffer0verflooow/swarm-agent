---
name: smart-pipe
description: 工具输出必过 smart_pipe — 高信号过滤, 原始日志归档, token 节省 70-85%
tags: [token-economy, filtering, scanner, recon]
---
# Smart Pipe 技能 — 高信号输出流过滤

## 何时使用
- 执行任何批量工具 (ffuf/katana/httpx/nuclei/gau/dalfox/subfinder) 且输出可能超过
  30 行时。
- 需要把原始输出完整留档但只把高信号行带入 agent 上下文时。
- 任何扫描/枚举结果回传前。

## 用法
```bash
# 工具输出过 smart_pipe: 全量归档 + top-40 高信号展示
<tool_cmd> | python3 scripts/smart_pipe.py --target <SLUG> --tool <TOOL> --limit 40

# 从文件过滤
cat results.txt | python3 scripts/smart_pipe.py -t <SLUG> -n ffuf -l 20
```

## 打分启发式 (理解排序逻辑)
| 特征 | 加分 |
|------|------|
| 静态资源 (.png/.css/.woff 等) | 0 分 → 过滤 |
| 关键标记 (cve-/rce/idor/ssrf/xxe/auth bypass/[high]/[critical]) | +80 |
| 密钥标记 (.env/.git/swagger/graphql/jwt/bearer/token=) | +60 |
| HTTP 200 + /api/ 或 /v1/ | +25 +25 |
| 401/403 + /admin 或 /api/ | +20 +25 |
| 500 | +15 |
| 查询参数 (?a=b) | +20 |
| UUID | +20 |
| key/secret/tok/pass 且熵 > 3.8 | +30 |

## 铁律
1. **原始日志必须全量归档** — smart_pipe 自动写到 recon/<SLUG>/<tool>_raw.txt,
   禁止只靠过滤后的行做结论 (回查原始输出是验证关证据)。
2. **展示行 = 候选, 不是发现** — 高信号行仍需人工/后续阶段验证, 不因"被展示"
   而声称漏洞。
3. **报告引用必须可回查** — finding 里引用的 URL/状态码必须在 raw 归档中能找到。
4. 归档目录默认 <repo>/recon/<SLUG>/; 无 AGENTS.md 的项目根时用 --raw-dir 指定。

## 验证
- 过滤后 stdout 有 "📊 [Smart Filter] N high-signal findings prioritized" 计数行。
- recon/<SLUG>/<tool>_raw.txt 存在且行数 >= 输入非空行数。
