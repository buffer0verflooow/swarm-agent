# 蜂群技能与 MCP 自主化 (2026-08-12)

本设计让蜂群拥有**自己的**技能注册表和 MCP 客户端，不再依赖 Hermes 的
`--skills` 加载或 `config.yaml mcp_servers`。任何 executor（Hermes CLI、
Codex、Claude、opencode，只要带 shell）都能消费这两项能力。

## 当前状态 (2026-08-12)

- **技能：已启用。** `model_profiles.load_skills` 已指向 `skills/*.md`
  内容文件（scanner/analyst/exploiter/reporter/researcher/custom），worker
  领取任务时注入 `## Role Skills`。
- **MCP：未启用（短期不接线）。** 所有角色 `mcp_servers=[]`，worker 不会
  注入 `## MCP Tools` 段，`scripts/mcp_tool.py` 目前无调用方。example server
  与 CLI、测试保留为后续接入基线；在运营方显式执行
  `swarmctl models set --role <r> --mcp-servers '[...]'` 之前，不要假设 MCP
  已生效。

## 背景: 为什么要自己做

- **技能 (migration 008 → 009)**：008 只把 `load_skills` 的"方法论句子"
  作为名字清单注入 worker 上下文，真实技能内容依赖外部 executor 加载。
  2026-08-11 ablation（`reports/ablation-skill-packs-2026-08-11.md`）证明
  注入管线可用但句子型内容零增益。009 把默认条目改为技能**文件名**，
  worker 注入 `skills/*.md` 的真实内容——机制不变，内容变成可编辑文件，
  换内容不再改代码。
- **MCP**：此前蜂群完全没有 MCP 通道；若要用 MCP 工具只能靠 Hermes 的
  配置，等于把工具链绑死在 Hermes 上。009 增加 `mcp_servers` 列 +
  蜂群自持 stdio MCP 客户端（stdlib 实现，零新依赖）。

## 技能系统

### 目录与格式

```
swarm-knowledge/
├── skills/                  # 蜂群自有技能 (SWARM_SKILLS_DIR 可覆盖)
│   ├── scanner.md
│   ├── analyst.md
│   ├── exploiter.md
│   ├── reporter.md
│   ├── researcher.md        # research 产品线 (migration 016)
│   └── custom.md
```

`researcher` 是 research 产品线（竞品/调研/技术选型，migration 016）的独立
角色与技能：任务类型 `research` → 角色 `researcher` → 技能 `researcher.md`，
与二进制/漏洞分析（analyst）完全分离。

每个技能是带 frontmatter 的 markdown：

```markdown
---
name: analyst
description: 静态/动态分析 — 数据流、边界检查、证据分级、反例意识
tags: [static-analysis, binary, evidence]
---
# Analyst 技能
## 核心方法
- 静态分析: 先看数据流/边界检查/类型转换, 再下漏洞结论。
...
```

### 解析与注入

`src/swarm/skills.py` 提供：

| 函数 | 作用 |
|---|---|
| `discover_skills()` | 列出 skills/ 下所有技能 (name/description/tags/size) |
| `load_skill(name)` | 读取单个技能 (frontmatter + body) |
| `resolve_skill_ref(entry)` | 名称 → 大小写不敏感 → 标签 → None |
| `inject_skills_context(parts, load_skills)` | 把技能内容追加进 worker 上下文 |
| `import_skill(path)` | 从外部 (如 ~/.hermes/skills) 一次性导入技能 |

`load_skills` 条目解析顺序：**精确技能名 → 大小写不敏感名 → 标签匹配 →
旧条目原样透传**。旧库里的方法论句子（008 数据）解析不到技能文件时按
原样注入为 `## Role Skill Directives`，完全向后兼容。

### 预算控制

- 总预算 `SWARM_SKILL_BUDGET_CHARS`（默认 6000 字符），按技能数平分；
- 单块上限 `SWARM_SKILL_BLOCK_CHARS`（默认 3000 字符）；
- 超限内容截断并标注 `…(技能内容已截断, 详见文件)`。

### 开关

- `SWARM_SKILL_PACKS=0`：完全禁用技能注入（ablation 基线组语义保留）。
- `SWARM_SKILLS_DIR=/path`：指向别的技能目录（测试/实验用）。

### CLI

```
python3 scripts/swarmctl.py skill list
python3 scripts/swarmctl.py skill show analyst
python3 scripts/swarmctl.py skill import ~/.hermes/skills/xxx/SKILL.md --name xxx
python3 scripts/swarmctl.py models set --role analyst --provider p --model m \
    --load-skills '["analyst","my-skill"]' --mcp-servers '["example"]'
```

## MCP 系统

### 配置: mcp_servers.json

仓库根目录（`SWARM_MCP_CONFIG` 可覆盖）：

```json
{
  "servers": {
    "example": {
      "command": "python3",
      "args": ["scripts/example_mcp_server.py"],
      "cwd": "/home/pwn/workspace/research/swarm-knowledge",
      "timeout": 15,
      "allow": ["add", "echo"],
      "deny": [],
      "tools": {
        "add": {"description": "整数加法, 返回 a+b"}
      }
    }
  }
}
```

- `command`/`args`/`env`/`cwd` 在**配置期固定**，worker 无法提供命令；
- `allow` 白名单限制可调工具，`deny` 黑名单兜底；
- `tools` 是静态工具描述（注入上下文用，不拉起进程）；缺省时 worker 被告知
  先运行 `mcp_tool.py list --live` 发现；
- `scripts/example_mcp_server.py` 是 stdio MCP 协议的参考实现（add/echo），
  测试直接复用它。

### 客户端: src/swarm/mcp_client.py

纯 stdlib（asyncio + subprocess）的 stdio JSON-RPC 2.0 客户端：

- `MCPClient`：initialize 握手 → tools/list → tools/call → close；
  每次调用一次性拉起服务器，不残留常驻进程；`timeout` 约束整个会话；
  握手失败自动回收子进程（无僵尸）。
- `MCPRegistry`：配置注册表 + allowlist 强制 + 静态工具描述。
- `registry_tool_prompt(server_names)`：**只读静态配置**生成 worker 上下文的
  `## MCP Tools` 段，不拉起任何进程。

### CLI: scripts/mcp_tool.py

```
python3 scripts/mcp_tool.py list                      # 静态工具清单
python3 scripts/mcp_tool.py list --server example --live   # 拉起服务器取声明
python3 scripts/mcp_tool.py call example add --args '{"a":1,"b":2}'
python3 scripts/mcp_tool.py health                     # 全服务器连通性
```

stdout 统一 JSON（含 `success` 字段），executor 可直接解析。工具输出即证据，
worker 需原样记录。

### Worker 接线

`build_task_context`（src/swarm/worker.py）在 claim 后追加两段：

1. `## Role Skills` — `load_skills` 解析出的技能文件内容；
2. `## MCP Tools` — `mcp_servers` 列对应的静态工具清单 + 调用格式。

注入全程 try/except 兜底，任何失败都不阻断任务。角色默认
`mcp_servers=[]`，行为不变；运营方按需用 `swarmctl models set --mcp-servers`
启用。

## 安全模型

| 面 | 约束 |
|---|---|
| 技能内容 | 只读 `skills/` 文件；导入需显式 `swarmctl skill import` |
| MCP 命令 | 配置期固定；worker 只能选 (server, tool, arguments) |
| 工具权限 | `allow` 白名单 / `deny` 黑名单，registry 强制 |
| 进程生命周期 | 一次性 spawn，timeout 兜底，异常路径 close |
| 注入失败 | 静默降级，不阻断任务 |

## 验证

```
.venv/bin/python -m pytest tests/test_swarm_skills.py tests/test_mcp_client.py tests/test_role_skill_packs.py -q
.venv/bin/python -m pytest tests/ -q                     # 全量回归
```

新增测试：`test_swarm_skills.py`（解析/解析顺序/预算/开关/导入/上下文注入）、
`test_mcp_client.py`（握手/调用/超时/allowlist/CLI/health，端到端打真实
stdio 服务器）。

## 遗留风险

1. **ablation 门**：机制已上线，但"领域事实型技能内容是否真的提升成功率"
   尚未复测——按 2026-08-11 结论，任何声称有效的内容须先过 ablation。
2. **MCP 生态**：只支持 stdio 传输；HTTP/SSE 类服务器需要后续实现。
3. **并发**：多次工具调用 = 多次服务器 spawn；高频率场景可加连接复用，
   但需权衡僵尸风险。
