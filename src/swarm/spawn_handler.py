"""
Spawn Handler — 桥接 Stigmergy 信号到实际 Agent 创建

spawn_requests 表中存的是"需要什么角色的Agent"信号。
本模块负责将信号转化为实际的 agent 实例，并注入 KB 上下文。

两种模式:
1. Hermes delegate_task 模式 (生产): 调用 delegate_task 生成子 agent
2. Mock 模式 (测试): 生成虚拟 agent_id 用于集成测试

用法:
    from src.swarm.spawn_handler import HermesSpawnHandler, MockSpawnHandler

    orch = SwarmOrchestrator(db)
    orch.set_spawn_handler(HermesSpawnHandler(db))
    asyncio.run(orch.run_loop("run-001"))
"""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Any, Callable, Dict, Optional

from .lifecycle import AgentLifecycle

_log = logging.getLogger("swarm_knowledge.spawn_handler")


class BaseSpawnHandler:
    """spawn_handler 基类。子类实现 create_agent()。"""

    def __init__(self, db):
        self.db = db

    async def __call__(self, spawn_request: dict, context: str) -> Optional[str]:
        """
        Orchestrator 调用此方法处理 spawn 请求。

        Args:
            spawn_request: spawn_requests 表的一行 (dict)
            context: Orchestrator 构建的 KB 上下文字符串

        Returns:
            agent_id (成功) 或 None (失败)
        """
        try:
            agent_id = await self.create_agent(spawn_request, context)
            if agent_id:
                # 注册 agent 生命周期
                role = spawn_request["requested_role"]
                lc = AgentLifecycle(self.db, agent_id, spawn_request["run_id"])
                lc.register(role=role, capabilities=self._get_capabilities(role))
                _log.info("SpawnHandler: registered %s as %s", agent_id[:8], role)
            return agent_id
        except Exception as e:
            _log.error("SpawnHandler: failed to create agent: %s", e)
            return None

    async def create_agent(self, spawn_request: dict, context: str) -> Optional[str]:
        """子类实现：实际创建 agent 的逻辑。"""
        raise NotImplementedError

    def _get_capabilities(self, role: str) -> list:
        """角色 → 能力映射"""
        caps = {
            "scanner": ["nmap", "nuclei", "ffuf", "masscan"],
            "analyst": ["reverse_engineering", "static_analysis", "code_review"],
            "exploiter": ["sqlmap", "metasploit", "burpsuite"],
            "reporter": ["writeup", "documentation"],
            "orchestrator": ["delegation", "scheduling"],
        }
        return caps.get(role, [])


class MockSpawnHandler(BaseSpawnHandler):
    """测试用 mock handler — 生成虚拟 agent_id。"""

    def __init__(self, db, prefix: str = "mock-agent"):
        super().__init__(db)
        self.prefix = prefix
        self.counter = 0

    async def create_agent(self, spawn_request: dict, context: str) -> str:
        self.counter += 1
        agent_id = f"{self.prefix}-{self.counter:04d}-{uuid.uuid4().hex[:8]}"
        _log.info("MockSpawnHandler: created %s for role=%s", agent_id, spawn_request["requested_role"])
        return agent_id


class HermesSpawnHandler(BaseSpawnHandler):
    """
    生产用 handler — 通过 Hermes delegate_task 创建子 agent。

    在 Hermes 环境中，spawn = delegate_task(goal=..., context=...)。
    本 handler 构建 role-specific 的 goal 字符串，调用 delegate_task。

    用法:
        handler = HermesSpawnHandler(db)
        orch.set_spawn_handler(handler)

    注意: delegate_task 是 Hermes Agent 的内置工具，不在普通 Python
    环境中可用。在非 Hermes 环境中使用 MockSpawnHandler 或自定义 handler。
    """

    # 角色 → goal 模板
    GOAL_TEMPLATES = {
        "scanner": (
            "你是蜂群 scanner agent。你的任务是执行以下扫描工作:\n"
            "{reason}\n\n"
            "上下文:\n{context}\n\n"
            "## Phase 0: 环境预检 + 技术栈指纹（耗时 <30s）\n"
            "1. DNS检查: dig +short 目标域名，如果返回 198.18.x.x 或 10.x.x.x → 跳过nmap(代理环境)\n"
            "2. HTTP指纹: curl -sI 每个目标 → 检测 Server/Content-Type/X-Powered-By\n"
            "3. SPA检测: 如果 Content-Type=text/html 且 body 含 <div id=\\\"root\\\"> 或 __NEXT_DATA__ → 标记为SPA，跳过ffuf目录爆破\n"
            "4. API检测: 如果 Content-Type=application/json → 跳过ffuf，改用端点枚举\n"
            "5. 根据指纹结果选择工具:\n"
            "   - 非代理环境 + 非Cloudflare → nmap -F --top-ports 200\n"
            "   - 非SPA → ffuf 目录爆破\n"
            "   - 仅对非SPA目标运行 nuclei（限 -t exposures/ -t misconfiguration/ -t cves/）\n"
            "\n"
            "## Phase 1: 广度侦察\n"
            "1. 使用选中工具执行扫描\n"
            "2. 每测试一个端点/参数后，记录探索轨迹:\n"
            "   python3 ~/workspace/research/swarm-knowledge/exploration_trace.py \\\n"
            "     --target-url 'ACTUAL_URL_TESTED' --method GET \\\n"
            "     --vuln-class 'IDOR' --result 'not_found' --depth 'medium' \\\n"
            "     --agent '{agent_label}' --run-id '{run_id}' \\\n"
            "     --notes 'brief description of what was attempted'\n"
            "3. 发现漏洞时，先 capture 再 trace (result=found):\n"
            "   python3 ~/workspace/research/swarm-knowledge/capture.py "
            "  --content '发现描述' --agent '{agent_label}' "
            "  --source task_result --run-id '{run_id}' --task-id '{parent_task_id}' "
            "  --tags 'recon,scan' --force-capture\n"
            "   python3 ~/workspace/research/swarm-knowledge/exploration_trace.py \\\n"
            "     --target-url 'URL' --vuln-class 'TYPE' --result 'found' \\\n"
            "     --finding-id 'CAPTURED_ID_FROM_ABOVE' --depth 'deep' \\\n"
            "     --agent '{agent_label}' --run-id '{run_id}'\n"
            "4. 如果收到 429 (Cloudflare限速)，记录阻塞:\n"
            "   python3 ~/workspace/research/swarm-knowledge/exploration_trace.py \\\n"
            "     --target-url 'URL' --vuln-class 'BLOCKED_TYPE' --result 'blocked' \\\n"
            "     --agent '{agent_label}' --run-id '{run_id}' --notes 'Cloudflare 429 rate limit'\n"
            "   限速后等待30s再继续\n"
            "5. 如果发现高价值目标，capture 会自动触发 spawn\n"
            "6. 每30秒发心跳: lc.beat(current_task_id=..., load=0.5)"
        ),
        "exploiter": (
            "你是蜂群 exploiter agent。你的任务是利用以下发现:\n"
            "{reason}\n\n"
            "上下文:\n{context}\n\n"
            "要求:\n"
            "0. 启动后先从共享任务市场领取 exploiter 工作，不等待 analyst 手工交接；"
            "领取 JSON 中的 model_profile 是蜂群选择的模型/工具策略，Hermes 只负责承载执行:\n"
            "   python3 ~/workspace/research/swarm-knowledge/agent_worker.py "
            "--run-id '{run_id}' --agent '{agent_label}' --role exploiter --claim-only\n"
            "1. 基于上下文中的漏洞发现，尝试利用\n"
            "2. 使用 sqlmap/metasploit/burpsuite 等工具\n"
            "3. 每个利用尝试通过 capture.py --force-capture 写入知识库\n"
            "4. 完成任务后用 agent_worker.py --complete-task-id <task_id> --content '结果摘要' 标记完成；若有文件追加 --artifact <共享路径>\n"
            "5. 利用成功后标记为 vulnerability 类型\n"
            "6. chain_depth={chain_depth}, 不超过 max_chain_depth={max_chain_depth}"
        ),
        "analyst": (
            "你是蜂群 analyst agent。你的任务是分析以下内容:\n"
            "{reason}\n\n"
            "上下文:\n{context}\n\n"
            "要求:\n"
            "0. 启动后先从共享任务市场领取 analyst 工作，不等待 scanner 完整结束；"
            "领取 JSON 中的 model_profile 是蜂群选择的模型/工具策略，Hermes 只负责承载执行:\n"
            "   python3 ~/workspace/research/swarm-knowledge/agent_worker.py "
            "--run-id '{run_id}' --agent '{agent_label}' --role analyst --claim-only\n"
            "1. 对发现的端点/服务进行深度分析\n"
            "2. 反编译/反汇编/代码审计\n"
            "3. 分析结果通过 capture.py --force-capture 写入知识库\n"
            "4. 完成任务后用 agent_worker.py --complete-task-id <task_id> --content '结果摘要' 标记完成；若有文件追加 --artifact <共享路径>\n"
            "5. 发现攻击模式后 capture 会自动发布 exploiter 市场任务"
        ),
        "reporter": (
            "你是蜂群 reporter agent。你的任务是生成报告:\n"
            "{reason}\n\n"
            "上下文:\n{context}\n\n"
            "要求:\n"
            "1. 从知识库检索所有相关发现\n"
            "2. 按严重程度排序，生成结构化报告（含 Description/Steps/Impact/Remediation）\n"
            "3. ⚠️ 不要写文件！把最终报告直接放在你的回复文本中返回。\n"
            "   （delegate_task sandbox 内写入的文件不会出现在主机磁盘上，请用回复文本传递报告内容）\n"
            "4. 报告内容通过 capture.py --force-capture 同时写入知识库:\n"
            "   python3 ~/workspace/research/swarm-knowledge/capture.py "
            "  --content '报告摘要(前500字)' --agent '{agent_label}' "
            "  --source task_result --tags 'report,final' --force-capture"
        ),
    }

    def __init__(self, db, delegate_fn: Callable = None):
        """
        Args:
            db: SwarmDB 实例
            delegate_fn: delegate_task 函数。在 Hermes 环境中传入。
                        签名: delegate_fn(goal: str, context: str, ...) -> str
                        如果为 None，会在运行时尝试 import。
        """
        super().__init__(db)
        self.delegate_fn = delegate_fn

    async def create_agent(self, spawn_request: dict, context: str) -> Optional[str]:
        role = spawn_request["requested_role"]
        reason = spawn_request["reason"] if spawn_request["reason"] else ""
        chain_depth = spawn_request["chain_depth"] if spawn_request["chain_depth"] else 0
        max_chain_depth = spawn_request["max_chain_depth"] if spawn_request["max_chain_depth"] else 3
        fallback_agent_id = str(uuid.uuid4())
        parent_task_id = spawn_request.get("parent_task_id") or ""

        template = self.GOAL_TEMPLATES.get(role, self.GOAL_TEMPLATES["scanner"])
        goal = template.format(
            reason=reason,
            context=context,
            chain_depth=chain_depth,
            max_chain_depth=max_chain_depth,
            agent_label=f"{role}-{fallback_agent_id[:8]}",
            run_id=spawn_request["run_id"],
            parent_task_id=parent_task_id,
            role=role,
        )

        if not self.delegate_fn:
            _log.warning("HermesSpawnHandler: no delegate_fn; refusing to mark spawn as created")
            return None

        try:
            result = self.delegate_fn(goal=goal, context=context)
            if inspect.isawaitable(result):
                result = await result
            agent_id = self._extract_agent_id(result) or fallback_agent_id
            _log.info("HermesSpawnHandler: delegate_task returned for %s", agent_id[:8])
            return agent_id
        except Exception as e:
            _log.error("HermesSpawnHandler: delegate_task failed: %s", e)
            return None

    def _extract_agent_id(self, result: Any) -> Optional[str]:
        """从 delegate_task 返回值中提取 agent id。"""
        if not result:
            return None
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("agent_id", "id", "task_id", "request_id"):
                value = result.get(key)
                if value:
                    return str(value)
        for key in ("agent_id", "id", "task_id", "request_id"):
            value = getattr(result, key, None)
            if value:
                return str(value)
        return None


def create_default_spawn_handler(db, mode: str = "mock") -> BaseSpawnHandler:
    """
    便捷函数: 创建默认 spawn handler。

    Args:
        db: SwarmDB 实例
        mode: "mock" (测试) 或 "hermes" (生产)

    Returns:
        spawn handler 实例
    """
    if mode == "hermes":
        return HermesSpawnHandler(db)
    return MockSpawnHandler(db)
