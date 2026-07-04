"""
Swarm Orchestrator — 蜂群主循环

职责：
  1. 每 5s  轮询 spawn_requests → 生成新 Agent
  2. 每 10s 轮询 agent_heartbeats → 清理僵尸 Agent
  3. 每 60s 运行治理循环 → DIKW 提升 + 衰减
  4. Agent 启动时注入 KB 上下文

这是一种「定时器驱动的编排器」，不依赖外部事件系统。
所有状态变化通过 SQLite 轮询感知——简单、可恢复、可调试。

用法:
    from src.swarm.orchestrator import SwarmOrchestrator
    import asyncio

    orch = SwarmOrchestrator(db)
    asyncio.run(orch.run_loop("run-001"))
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .lifecycle import cleanup_stale_agents, get_live_agents
from .spawner import (
    poll_spawn_requests,
    mark_spawn_fulfilled,
    mark_spawn_rejected,
    expire_old_requests,
    merge_duplicate_requests,
)

_log = logging.getLogger("swarm_knowledge.orchestrator")

# 默认轮询间隔
POLL_SPAWN_SEC = 5
POLL_HEARTBEAT_SEC = 10
POLL_GOVERNANCE_SEC = 60
POLL_POWER_SCHEDULE_SEC = 15  # 新: power schedule 轮询间隔
MAX_AGENTS_PER_RUN = 8  # 单次 run 最多同时活跃 Agent 数

# Power schedule 参数
BUDGET_BREADTH_THRESHOLD = 0.3   # <30% 预算用尽 → breadth (广撒网)
BUDGET_DEPTH_THRESHOLD = 0.7      # >70% 预算用尽 → depth (集中深挖)
MAX_CHAIN_DEPTH_DEFAULT = 3       # 最大链深度 (防止无限追链)
VULN_DENSITY_THRESHOLD = 0.15     # >15% 知识是 vulnerability 类型 → 切换到 depth


class SwarmOrchestrator:
    """
    蜂群编排器。

    spawn_handler 是一个回调函数，签名:
        async def spawn_handler(spawn_request: dict, context: str) -> str:
            # 调用 Claude API 生成新 Agent
            return agent_id

    如果不提供 spawn_handler，Orchestrator 只做维护（心跳清理/治理），
    spawn 请求会被记录但不执行。
    """

    def __init__(self, db):
        self.db = db
        self.spawn_handler: Optional[Callable] = None
        self._stopped = False

    def set_spawn_handler(self, handler: Callable):
        """设置 Agent 生成回调（由外部集成提供）"""
        self.spawn_handler = handler

    async def run_loop(self, run_id: str, tick_interval: float = 1.0):
        """
        主循环。每次 tick 检查是否有到期任务。

        Args:
            run_id: 当前 swarm run
            tick_interval: 基础 tick 间隔（秒）
        """
        last_spawn = 0.0
        last_heartbeat = 0.0
        last_governance = 0.0
        last_power = 0.0

        _log.info("Orchestrator loop started for run_id=%s", run_id)
        self._stopped = False

        while not self._stopped:
            now = time.time()

            # ① Spawn 处理 (每 POLL_SPAWN_SEC)
            if now - last_spawn >= POLL_SPAWN_SEC:
                await self._tick_spawn(run_id)
                last_spawn = now

            # ② 心跳清理 (每 POLL_HEARTBEAT_SEC)
            if now - last_heartbeat >= POLL_HEARTBEAT_SEC:
                await self._tick_heartbeat(run_id)
                last_heartbeat = now

            # ③ 治理循环 (每 POLL_GOVERNANCE_SEC)
            if now - last_governance >= POLL_GOVERNANCE_SEC:
                await self._tick_governance(run_id)
                last_governance = now

            # ④ Power schedule (每 POLL_POWER_SCHEDULE_SEC)
            if now - last_power >= POLL_POWER_SCHEDULE_SEC:
                await self._tick_power_schedule(run_id)
                last_power = now

            await asyncio.sleep(tick_interval)

        _log.info("Orchestrator loop stopped for run_id=%s", run_id)

    def stop(self):
        """停止主循环"""
        self._stopped = True

    # ── Private ticks ──

    async def _tick_spawn(self, run_id: str):
        """处理待处理的 spawn 请求"""
        # 先去重
        merged = merge_duplicate_requests(self.db, run_id)
        if merged:
            _log.info("Spawn: merged %d duplicate requests for run=%s", merged, run_id)

        # 检查 Agent 数量上限 — 满了就跳过，不拒绝排队请求
        live_count = len(get_live_agents(self.db, run_id))
        available_slots = MAX_AGENTS_PER_RUN - live_count
        if available_slots <= 0:
            _log.debug("Spawn: no available slots (%d/%d), skipping tick", live_count, MAX_AGENTS_PER_RUN)
            return

        pending = poll_spawn_requests(self.db, run_id, limit=available_slots)

        for req in pending:
            if self.spawn_handler is None:
                _log.debug("Spawn: no handler set, marking as rejected: %s", req["requested_role"])
                mark_spawn_rejected(self.db, req["request_id"], "no_spawn_handler")
                continue

            try:
                # 为新 Agent 构建 KB 上下文
                context = self._build_spawn_context(req)
                agent_id = await self.spawn_handler(req, context)

                if agent_id:
                    mark_spawn_fulfilled(self.db, req["request_id"], agent_id)
                    self._log_behavior(run_id, "emergence",
                        f"{req['requesting_agent']} 触发生成 {req['requested_role']} ({agent_id[:8]})")
                    _log.info("Spawn: fulfilled %s → %s (%s)",
                              req["requested_role"], agent_id[:8], req["reason"][:60])
                else:
                    mark_spawn_rejected(self.db, req["request_id"], "handler_returned_none")
            except Exception as e:
                _log.error("Spawn: failed for %s: %s", req["requested_role"], e)
                mark_spawn_rejected(self.db, req["request_id"], str(e))

        # 清理过期
        expired = expire_old_requests(self.db)
        if expired:
            _log.info("Spawn: expired %d old requests", expired)

    async def _tick_heartbeat(self, run_id: str):
        """清理僵尸 Agent"""
        stale = await asyncio.to_thread(cleanup_stale_agents, self.db)
        if stale:
            _log.warning("Heartbeat: cleaned %d stale agents: %s", len(stale), [s[:8] for s in stale])
            await asyncio.to_thread(
                self._log_behavior, run_id, "adaptation",
                f"清理僵尸 Agent: {[s[:8] for s in stale]}",
                stale,
            )

    async def _tick_governance(self, run_id: str):
        """运行治理循环（DIKW 提升 + 衰减 + 聚类 + 验证 + Wisdom蒸馏 + 本体发现）"""
        try:
            from src.governance.engine import (
                run_promotion_cycle, check_and_decay, run_pheromone_decay, auto_distill_strategies
            )
            from src.governance.wisdom import distill_wisdom
            from src.governance.verification import auto_enqueue_validations, process_validation_queue
            from src.ontology.discovery import discover_relations_from_cooccurrence

            result = await asyncio.to_thread(run_promotion_cycle, self.db)
            promote_count = result.get("promoted", 0) if isinstance(result, dict) else result
            if promote_count:
                _log.info("Governance: promoted %s entries", promote_count)
                await asyncio.to_thread(
                    self._log_behavior, run_id, "optimization",
                    f"DIKW 提升: {promote_count} 条知识升级",
                )

            decay_count = await asyncio.to_thread(check_and_decay, self.db)
            if decay_count:
                _log.info("Governance: decayed %d stale entries", decay_count)

            # 信息素衰减
            phero_result = await asyncio.to_thread(run_pheromone_decay, self.db)
            if phero_result.get("decayed") or phero_result.get("stale_marked"):
                _log.info("Governance: pheromone decayed %d, stale %d",
                          len(phero_result.get("decayed", [])),
                          len(phero_result.get("stale_marked", [])))

            # 策略自动蒸馏
            distill_result = await asyncio.to_thread(auto_distill_strategies, self.db)
            if distill_result.get("distilled"):
                _log.info("Governance: auto-distilled %d strategies", len(distill_result["distilled"]))

            # 独立验证 pipeline
            await asyncio.to_thread(auto_enqueue_validations, self.db, run_id)
            verify_result = await asyncio.to_thread(process_validation_queue, self.db)
            if verify_result.get("processed", 0) > 0:
                _log.info("Governance: verified %d (confirmed=%d, refuted=%d)",
                          verify_result["processed"],
                          verify_result.get("confirmed", 0),
                          verify_result.get("refuted", 0))
                await asyncio.to_thread(
                    self._log_behavior, run_id, "optimization",
                    f"独立验证: {verify_result['processed']}条 "
                    f"(确认={verify_result.get('confirmed',0)}, 反驳={verify_result.get('refuted',0)})",
                )

            # Wisdom 蒸馏 (L4 → distilled_rules)
            wisdom_result = await asyncio.to_thread(distill_wisdom, self.db)
            if wisdom_result.get("distilled"):
                _log.info("Governance: distilled %d wisdom rules", len(wisdom_result["distilled"]))
                await asyncio.to_thread(
                    self._log_behavior, run_id, "emergence",
                    f"Wisdom 蒸馏: {len(wisdom_result['distilled'])} 条规则",
                )

            # Ontology 关系自动发现
            onto_result = await asyncio.to_thread(discover_relations_from_cooccurrence, self.db)
            if onto_result.get("discovered"):
                _log.info("Governance: discovered %d ontology relations",
                          len(onto_result["discovered"]))
                await asyncio.to_thread(
                    self._log_behavior, run_id, "emergence",
                    f"本体发现: {len(onto_result['discovered'])} 条新关系",
                )

        except Exception as e:
            _log.warning("Governance: failed: %s", e)

    async def _tick_power_schedule(self, run_id: str):
        """
        Power schedule — AFLFast 风格的预算分配。
        
        每 15s 评估当前 run 的预算消耗和发现密度，动态调整策略:
        - 预算 <30%: breadth (广撒网，多 scanner)
        - 预算 30-70%: balanced (默认)
        - 预算 >70%: depth (集中深挖高价值目标)
        - 漏洞密度 >15%: 强制切换到 depth
        
        同时检查链深度，超过 max_chain_depth 的 spawn 请求被拒绝。
        """
        try:
            # 获取 run 统计
            run = self.db.fetch_one(
                "SELECT token_budget, tokens_spent, budget_strategy FROM swarm_runs WHERE run_id = ?",
                (run_id,),
            )
            if not run:
                return

            budget = run["token_budget"] or 100000
            spent = run["tokens_spent"] or 0
            budget_ratio = spent / budget if budget > 0 else 0

            # 计算漏洞密度
            vuln_count = self.db.fetch_one(
                "SELECT COUNT(*) AS c FROM knowledge_entries WHERE source_run_id = ? AND knowledge_type = 'vulnerability' AND status = 'active'",
                (run_id,),
            )
            total_entries = self.db.fetch_one(
                "SELECT COUNT(*) AS c FROM knowledge_entries WHERE source_run_id = ? AND status = 'active'",
                (run_id,),
            )
            vc = vuln_count["c"] if vuln_count else 0
            tc = total_entries["c"] if total_entries else 0
            vuln_density = vc / tc if tc > 0 else 0

            # 决定策略
            old_strategy = run["budget_strategy"] or "balanced"
            new_strategy = old_strategy

            if vuln_density > VULN_DENSITY_THRESHOLD and budget_ratio > BUDGET_BREADTH_THRESHOLD:
                new_strategy = "depth"  # 发现大量漏洞 → 集中深挖
            elif budget_ratio < BUDGET_BREADTH_THRESHOLD:
                new_strategy = "breadth"
            elif budget_ratio > BUDGET_DEPTH_THRESHOLD:
                new_strategy = "depth"
            else:
                new_strategy = "balanced"

            if new_strategy != old_strategy:
                self.db.execute(
                    "UPDATE swarm_runs SET budget_strategy = ? WHERE run_id = ?",
                    (new_strategy, run_id),
                )
                self.db.conn.commit()
                _log.info("PowerSchedule: %s → %s (budget=%.0f%%, vuln_density=%.0f%%)",
                          old_strategy, new_strategy, budget_ratio * 100, vuln_density * 100)
                self._log_behavior(run_id, "adaptation",
                    f"策略切换: {old_strategy} → {new_strategy} (预算={budget_ratio:.0%}, 漏洞密度={vuln_density:.0%})")

            # 链深度控制: 拒超过 max_chain_depth 的 spawn 请求
            # 每个 spawn 请求有自己的 max_chain_depth
            deep_requests = self.db.fetch_all(
                "SELECT request_id, chain_depth, max_chain_depth FROM spawn_requests WHERE run_id = ? AND status = 'pending' AND chain_depth > max_chain_depth",
                (run_id,),
            )
            for req in deep_requests:
                mark_spawn_rejected(self.db, req["request_id"], f"chain_depth_exceeded({req['chain_depth']}>{req['max_chain_depth']})")
                _log.info("PowerSchedule: rejected spawn %s (chain_depth=%d > max=%d)",
                          req["request_id"][:8], req["chain_depth"], req["max_chain_depth"])

            # 负载均衡: 找空闲 agent 分配高优先级 pending tasks
            await self._balance_load(run_id)

        except Exception as e:
            _log.warning("PowerSchedule: failed: %s", e)

    async def _balance_load(self, run_id: str):
        """
        工作窃取式负载均衡。
        
        读取 agent_heartbeats.load_score:
        - load_score < 0.3 的 agent 标记为可窃取
        - 找到 pending tasks 分配给空闲 agent
        """
        live_agents = get_live_agents(self.db, run_id)
        if not live_agents:
            return

        # 找空闲 agent (load < 0.3, stealable=1)
        idle_agents = [a for a in live_agents if (a.get("load_score") or 0) < 0.3]
        if not idle_agents:
            return

        # 找 pending tasks 未分配 agent
        pending_tasks = self.db.fetch_all(
            "SELECT task_id, task_type, focus_params FROM agent_tasks WHERE run_id = ? AND status = 'pending' AND agent_id IS NULL ORDER BY created_at LIMIT ?",
            (run_id, len(idle_agents)),
        )
        if not pending_tasks:
            return

        assigned = 0
        for task, agent in zip(pending_tasks, idle_agents):
            self.db.execute(
                "UPDATE agent_tasks SET agent_id = ?, status = 'running', started_at = datetime('now') WHERE task_id = ?",
                (agent["agent_id"], task["task_id"]),
            )
            assigned += 1

        if assigned:
            self.db.conn.commit()
            _log.info("LoadBalance: assigned %d tasks to idle agents", assigned)
            self._log_behavior(run_id, "optimization",
                f"负载均衡: 分配 {assigned} 个待处理任务给空闲 agent")

    def _build_spawn_context(self, req: dict) -> str:
        """
        为新 Agent 构建 KB 上下文注入。
        从触发 spawn 的知识条目中提取相关信息。
        """
        context_entry_ids = req.get("context_entry_ids", "[]")
        try:
            entry_ids = json.loads(context_entry_ids) if isinstance(context_entry_ids, str) else context_entry_ids
        except (json.JSONDecodeError, TypeError):
            entry_ids = []

        parts = [f"## 触发原因\n{req['reason']}"]

        # 注入触发知识条目的内容
        if entry_ids:
            parts.append("\n## 触发上下文（来自 KB）")
            for eid in entry_ids[:3]:
                row = self.db.fetch_one(
                    "SELECT title, content, knowledge_type, level FROM knowledge_entries WHERE id = ?",
                    (eid,),
                )
                if row:
                    parts.append(f"\n### [{row['knowledge_type']}] L{row['level']}: {row['title']}")
                    parts.append(row["content"][:500])

        # 注入相关策略规则
        try:
            from src.agents.retrieval import get_active_rules
            rules = get_active_rules(self.db, agent_role=req["requested_role"], limit=3)
            if rules:
                parts.append("\n## 已知策略规则")
                for r in rules:
                    parts.append(f"- **{r['rule_name']}** (p={r['priority']}): {r['rule_body'][:200]}")
        except Exception:
            pass

        return "\n".join(parts)

    def _log_behavior(self, run_id: str, behavior_type: str, description: str, agents: list = None):
        """记录涌现行为到 swarm_behaviors 表"""
        self.db.execute(
            """INSERT INTO swarm_behaviors
               (behavior_id, run_id, behavior_type, description, trigger_agents)
               VALUES (?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), run_id, behavior_type, description,
             json.dumps(agents or [])),
        )
        self.db.conn.commit()


# ── 便捷函数 ──

def create_orchestrator(db, spawn_handler: Callable = None) -> SwarmOrchestrator:
    """创建 Orchestrator 实例"""
    orch = SwarmOrchestrator(db)
    if spawn_handler:
        orch.set_spawn_handler(spawn_handler)
    return orch
