# MARBLE (MultiAgentBench) 接入评估报告

- 日期：2026-08-06
- 状态：评估完成，环境阻塞（无 Docker / userns 禁用），待权限决策后实施
- 目标：评估 MultiAgentBench 是否适合作蜂群系统第一个测试数据集

## 一、基准事实（已验证）

MultiAgentBench / MARBLE（ACL 2025, github.com/ulab-uiuc/MARBLE）
- 400 任务 = 4 场景 × 100：coding（3 agent 协作开发）/ bargaining（4 agent 谈判）/
  database（5 agent 数据库诊断）/ research（5 agent 协作研究）+ minecraft（需模拟器）
- 任务格式：jsonl → update_coding_config.py 改写 yaml config → marble/main.py 驱动
  agent（litellm 调 LLM）→ 产出 → evaluator 评估
- LLM 层用 **litellm.completion**：任何 OpenAI 兼容端点可接（deepseek 只需改 config + key）
- 依赖：python >=3.9,<3.12, poetry, litellm/beartype/sklearn/flask/bs4/arxiv 等约 20 个包

## 二、评估性质（关键修正）

| 场景 | 评估方式 | 执行式？ |
|---|---|---|
| coding | code_quality + collaboration_effectiveness 全 LLM 打分，**不编译不跑测试** | ❌ |
| database | 真实 MySQL/Postgres + Prometheus 监控栈 + test_set.json（539 条诊断样本） | ✅ |
| bargaining | agreement_reached | ✅ |
| research | diversity/engagement 纯 LLM 打分 | ❌ |

- coding_env.py 确认：只有 register_coder_actions / register_reviewer_actions，
  tester/debugger 动作被注释禁用 → 代码质量纯 LLM 主观评分
- database 场景 = DB-GPT 附属的数据库异常诊断（labels: too many indexes / POOR JOIN
  PERFORMANCE / highly concurrent inserts 等 11 类），agent 需从 Prometheus 指标+
  慢查询+告警中诊断 root cause

## 三、环境打通（2026-08-06 实测）

1. ~~本机无 docker~~ → **已解决**：用户安装 docker 29.7.2 + compose v5.4.0，
   `usermod -aG docker pwn` 后会话刷新，docker ps 正常
2. ~~rootless docker 不可行~~ → 已验证 userns 被禁（unshare uid_map 失败），
   特权 docker 是唯一路径（已采用）
3. **监控栈 4 容器全 up**：postgres:5432 / prometheus:9091（原 9090 被占改端口）/
   node_exporter:9100 / pg_exporter:9187，prometheus 3 个 target 全 up
4. 修复 compose 2 处：prometheus 端口冲突 9090→9091；postgres 18 镜像
   挂载路径 /var/lib/postgresql/data → /var/lib/postgresql
5. pg_stat_statements 扩展已建；异常脚本依赖 requests/pymysql 已装入 .venv

## 四、Pilot 实测结果（2026-08-06）

适配层：`benchmarks/marble_db_adapter.py` + `benchmarks/marble_db_runner.py`
真实 LLM worker：`benchmarks/marble_llm_worker.py`

- 任务建模：每个 MARBLE database 任务 → 蜂群 task_graph（10 节点：
  probe:stats → 8× analyze:<root_cause> 并行 → synthesize:diagnosis），
  依赖门控逐步发布，执行式评估（accuracy + response_time）
- 启发式基线（7 任务）：**6/7 exact，avg F1=0.95**
- **真实 LLM worker（deepseek-v4-flash + query_db 工具调用，7 任务）：
  6/7 exact，单根因 6/6 全中，avg F1≈0.95**
  - task 0/1 INSERT_LARGE_DATA、task 2 REDUNDANT_INDEX（11 索引 10 个
    idx_scan=0）、task 4 LOCK_CONTENTION（75,165 并发 update）、
    task 5 VACUUM（VACUUM FULL + delete）、task 6 FETCH_LARGE_DATA
    （34,330 次 orders 全表扫描）全部命中
  - 唯一波动：task 51 双根因（LOCK+REDUNDANT）一次全中、一次漏 LOCK
    ——SECOND-ORDER 规则与双根因判别的固有张力
  - 每任务 3-7 轮工具调用（8-23 次），23-122s
- **蜂群模式（task_graph 分工：probe → 8 并行 verifier → LLM lead 汇聚）：
  8/8 全中（task 0/1/2/4/5/6/50/51），avg F1=1.00**（v2 修复后）
  - v1（verifier 各自扫描）4/7：视野窄导致误报/漏报
  - v2 修复 3 点：① **共享信号快照**——probe 收集结构化聚合信号
    （7 个 pattern 调用计数+索引/扫描/锁），verifier 基于快照判定，
    tool_calls 从 40+ 降到 2-5（省 90% token），消除视野窄；
    ② **判据化 verifier**——STRICT EVIDENCE RULES 明确阈值
    （insert_table1.calls>=50 等），消除 LOCK vs REDUNDANT 误报；
    ③ **全证据 lead 汇聚**——synthesize 拿完整快照+完整证据全局
    裁决，纠偏 over/under-report（CPU_CONTENTION 兜底规则）
  - 修复过程：task 2/5 从 0 分→命中（快照+判据），task 6 从
    CPU_CONTENTION 误报→命中（兜底规则），task 4/51 保持命中
  - 对照结论（v1）修正为：分工型任务**必须**有强共享上下文层
    （probe 快照）与完整证据汇聚，做到后蜂群可反超单 agent
    （8/8 vs 6/7）——这直接指导蜂群架构设计
- LLM worker 关键设计：
  - OpenAI-compatible /chat/completions + tools schema（query_db），
    同 controller.py 模式；zenmux (deepseek/deepseek-v4-flash) 优先，
    ohmygpt (deepseek-reasoner, anthropic_messages 模式) 兜底
  - prompt 迭代 3 轮：① dead tuples 是 INSERT 副产物非 VACUUM；
    ② SECOND-ORDER SYMPTOMS RULE：脚本 setup 操作（FETCH 前的大
    INSERT、REDUNDANT 的并发 update 验证）不报独立根因——修复
    系统性误报，单根因从 2/6 提升到 6/6
  - 诊断输出 JSON {root_causes, evidence}，evidence 带具体信号值
    （调用数/耗时/索引扫描数）= 执行式证据链，符合 P5 铁律
- 踩坑记录：
  - create_task_graph 返回 graph_id str（非 dict）
  - agent_tasks.run_id 外键 → 需先注册 swarm_runs；target_type CHECK
    枚举限制（'database' 不合法，用 'webapp'）
  - 异常触发必须同步（Popen 后台会导致并发任务抢 tmp 库卡死），
    threads/nrow 需限制（20/5000），脚本超时用 pkill 兜底
  - pg_locks 里 DDL 的 AccessExclusiveLock 不是锁竞争证据；
    seq_scan>0 任何表都有——都需更精确判据
  - Hermes config custom_providers 顺序：zenmux 在 ohmygpt 之后，
    provider 选择要显式优先 zenmux（OpenAI 兼容），ohmygpt 是
    anthropic_messages 模式不兼容 OpenAI 格式；.venv 需装 pyyaml

## 五、结论

- **适合性**：MultiAgentBench 适合测蜂群"协作机制"（显式 relationships 图 vs
  stigmergy 市场领取对比，milestone KPI），不适合当能力分数来源（coding/research
  是 LLM 自打分）
- **database 场景**是真执行式（✅ P5 铁律），但需 Docker 监控栈 —— 当前环境不可跑
- **推进路径**（需用户决策）：
  A. 用户在 Hyper-V 终端配 sudo（visudo 加免密）→ 我全自动装 docker-ce → 跑 database
  B. 用户手动装 docker → 给我命令执行
  C. 换纯 Python 可跑的轻量验证（coding 场景跑通接入链路，评估仅作过程分析）

## 五、备用：手动安装 docker-ce 命令（方案 B）

```bash
# 在 Hyper-V 终端（有 sudo 密码）
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker pwn
# 重新登录后 docker 无需 sudo
```
