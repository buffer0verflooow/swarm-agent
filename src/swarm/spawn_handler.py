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

import asyncio
import json
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
            "要求:\n"
            "1. 使用 nmap/nuclei/ffuf 等工具执行扫描\n"
            "2. 发现的结果通过 capture.py 写入知识库:\n"
            "   python3 ~/workspace/research/swarm-knowledge/capture.py "
            "  --content '发现描述' --agent 'scanner-{agent_id[:8]}' "
            "  --source task_result --tags 'recon,scan'\n"
            "3. 如果发现高价值目标(开放端口/漏洞)，capture 会自动触发 spawn\n"
            "4. 每30秒发心跳: lc.beat(current_task_id=..., load=0.5)"
        ),
        "exploiter": (
            "你是蜂群 exploiter agent。你的任务是利用以下发现:\n"
            "{reason}\n\n"
            "上下文:\n{context}\n\n"
            "要求:\n"
            "1. 基于上下文中的漏洞发现，尝试利用\n"
            "2. 使用 sqlmap/metasploit/burpsuite 等工具\n"
            "3. 每个利用尝试通过 capture.py 写入知识库\n"
            "4. 利用成功后标记为 vulnerability 类型\n"
            "5. chain_depth={chain_depth}, 不超过 max_chain_depth={max_chain_depth}"
        ),
        "analyst": (
            "你是蜂群 analyst agent。你的任务是分析以下内容:\n"
            "{reason}\n\n"
            "上下文:\n{context}\n\n"
            "要求:\n"
            "1. 对发现的端点/服务进行深度分析\n"
            "2. 反编译/反汇编/代码审计\n"
            "3. 分析结果通过 capture.py 写入知识库\n"
            "4. 发现攻击模式后 capture 会自动触发 exploiter spawn"
        ),
        "reporter": (
            "你是蜂群 reporter agent。你的任务是生成报告:\n"
            "{reason}\n\n"
            "上下文:\n{context}\n\n"
            "要求:\n"
            "1. 从知识库检索所有相关发现\n"
            "2. 按严重程度排序，生成结构化报告\n"
            "3. 报告通过 capture.py 写入知识库 (knowledge_type=strategy)"
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

        template = self.GOAL_TEMPLATES.get(role, self.GOAL_TEMPLATES["scanner"])
        goal = template.format(
            reason=reason,
            context=context,
            chain_depth=chain_depth,
            max_chain_depth=max_chain_depth,
            agent_id="",
        )

        agent_id = str(uuid.uuid4())

        if self.delegate_fn:
            # 实际调用 delegate_task
            try:
                result = self.delegate_fn(goal=goal, context=context)
                _log.info("HermesSpawnHandler: delegate_task returned for %s", agent_id[:8])
            except Exception as e:
                _log.error("HermesSpawnHandler: delegate_task failed: %s", e)
                return None
        else:
            _log.info("HermesSpawnHandler: no delegate_fn, agent %s registered (goal not dispatched)",
                      agent_id[:8])

        return agent_id


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
