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
    request_spawn,
    build_spawn_dedup_key,
    claim_spawn_requests,
    mark_spawn_fulfilled,
    mark_spawn_rejected,
    expire_old_requests,
    merge_duplicate_requests,
    recover_stale_spawn_claims,
)
from .work_queue import claim_work_tasks, poll_work_tasks, recover_stale_work_claims

_log = logging.getLogger("swarm_knowledge.orchestrator")

# 默认轮询间隔
POLL_SPAWN_SEC = 5
POLL_WORK_SEC = 2
POLL_HEARTBEAT_SEC = 10
POLL_GOVERNANCE_SEC = 60
POLL_POWER_SCHEDULE_SEC = 15  # 新: power schedule 轮询间隔
POLL_CONTROLLER_SEC = 60      # Controller LLM 判决间隔 (Phase B)
POLL_HEALTH_SEC = 30          # 健康检查间隔
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
        last_work = 0.0
        last_heartbeat = 0.0
        last_governance = 0.0
        last_power = 0.0
        last_controller = 0.0  # Phase B
        last_health = 0.0      # health check

        _log.info("Orchestrator loop started for run_id=%s", run_id)
        self._stopped = False

        while not self._stopped:
            now = time.time()

            # ① Spawn 处理 (每 POLL_SPAWN_SEC)
            if now - last_spawn >= POLL_SPAWN_SEC:
                await self._safe_tick("spawn", self._tick_spawn, run_id)
                await self._safe_tick("stigmergy_spawn", self._tick_stigmergy_spawn, run_id)
                last_spawn = now

            # ② 任务市场维护 (每 POLL_WORK_SEC)
            if now - last_work >= POLL_WORK_SEC:
                await self._safe_tick("work_market", self._tick_work_market, run_id)
                last_work = now

            # ③ 心跳清理 (每 POLL_HEARTBEAT_SEC)
            if now - last_heartbeat >= POLL_HEARTBEAT_SEC:
                await self._safe_tick("heartbeat", self._tick_heartbeat, run_id)
                last_heartbeat = now

            # ④ 治理循环 (每 POLL_GOVERNANCE_SEC)
            if now - last_governance >= POLL_GOVERNANCE_SEC:
                await self._safe_tick("governance", self._tick_governance, run_id)
                last_governance = now

            # ⑤ Power schedule (每 POLL_POWER_SCHEDULE_SEC)
            if now - last_power >= POLL_POWER_SCHEDULE_SEC:
                await self._safe_tick("power_schedule", self._tick_power_schedule, run_id)
                last_power = now

            # ⑥ Controller (每 POLL_CONTROLLER_SEC) — Phase B
            if now - last_controller >= POLL_CONTROLLER_SEC:
                await self._safe_tick("controller", self._tick_controller, run_id)
                last_controller = now

            # ⑦ Health check (每 POLL_HEALTH_SEC)
            if now - last_health >= POLL_HEALTH_SEC:
                await self._safe_tick("health", self._tick_health, run_id)
                last_health = now

            await asyncio.sleep(tick_interval)

        _log.info("Orchestrator loop stopped for run_id=%s", run_id)

    def stop(self):
        """停止主循环"""
        self._stopped = True

    # ── Private ticks ──

    async def _safe_tick(self, tick_name: str, tick_fn: Callable, run_id: str):
        try:
            await tick_fn(run_id)
        except Exception:
            _log.exception("Orchestrator tick failed: %s run_id=%s", tick_name, run_id)

    async def _tick_spawn(self, run_id: str):
        """处理待处理的 spawn 请求"""
        expired = expire_old_requests(self.db)
        if expired:
            _log.info("Spawn: expired %d old requests", expired)

        recovered = recover_stale_spawn_claims(self.db)
        if recovered:
            _log.info("Spawn: recovered %d stale spawning claims", recovered)

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

        pending = claim_spawn_requests(self.db, run_id, limit=available_slots)

        for req in pending:
            if self.spawn_handler is None:
                _log.debug("Spawn: no handler set, marking as rejected: %s", req["requested_role"])
                mark_spawn_rejected(self.db, req["request_id"], "no_spawn_handler")
                continue

            try:
                # Phase C: detect worker_mode from reason marker
                reason = req.get("reason", "")
                if "[worker_mode]" in reason:
                    req["worker_mode"] = True
                    req["reason"] = reason.replace(" [worker_mode]", "")

                # 为新 Agent 构建 KB 上下文 (worker_mode 时跳过探索记忆)
                if req.get("worker_mode"):
                    context = self._build_spawn_context_worker(req)
                else:
                    context = self._build_spawn_context(req)
                agent_id = await self.spawn_handler(req, context)

                if agent_id:
                    fulfilled = mark_spawn_fulfilled(
                        self.db,
                        req["request_id"],
                        agent_id,
                        req.get("claim_token") or req.get("claimed_by"),
                    )
                    if not fulfilled:
                        _log.warning(
                            "Spawn: fulfillment rejected for %s due to claim token mismatch",
                            req["request_id"],
                        )
                        continue
                    self._log_behavior(run_id, "emergence",
                        f"{req['requesting_agent']} 触发生成 {req['requested_role']} ({agent_id[:8]})")
                    _log.info("Spawn: fulfilled %s → %s (%s)",
                              req["requested_role"], agent_id[:8], req["reason"][:60])
                else:
                    mark_spawn_rejected(self.db, req["request_id"], "handler_returned_none")
            except Exception as e:
                _log.error("Spawn: failed for %s: %s", req["requested_role"], e)
                mark_spawn_rejected(self.db, req["request_id"], str(e))

    # Stigmergy auto-spawn — 新漏洞/高价值发现 → 自动 spawn analyst/exploiter
    MAX_AUTO_SPAWN_PER_TICK = 3  # 每次 tick 最多自动 spawn 数
    AUTO_SPAWN_ROLE_MAP = {
        "vulnerability": ["analyst", "exploiter"],
        "vulnerability HIGH": ["exploiter"],
        "observation L3+": ["analyst"],
    }

    async def _tick_stigmergy_spawn(self, run_id: str):
        """检查新增的高价值知识条目，自动生成 spawn 请求。

        Phase 间 stigmergy: P1 发现漏洞 → 自动 spawn P2 analyst；analyst 确认漏洞 → 自动 spawn P3 exploiter。
        只在 live agent 未达上限时触发。
        """
        live_count = len(get_live_agents(self.db, run_id))
        available = MAX_AGENTS_PER_RUN - live_count
        if available <= 0:
            return

        # 查询本 run 新增的未处理高价值发现
        new_entries = self.db.fetch_all(
            """SELECT id, title, knowledge_type, level, domain, tags
               FROM knowledge_entries
               WHERE source_run_id = ?
                 AND status = 'active'
                 AND (knowledge_type = 'vulnerability' OR level >= 3)
               ORDER BY level DESC, created_at DESC
               LIMIT ?""",
            (run_id, self.MAX_AUTO_SPAWN_PER_TICK * 3),
        )

        spawned = 0
        for entry in new_entries:
            if spawned >= self.MAX_AUTO_SPAWN_PER_TICK:
                break

            # 决定 spawn 什么角色
            roles_to_spawn = self._auto_spawn_roles(entry)
            for role in roles_to_spawn:
                if spawned >= self.MAX_AUTO_SPAWN_PER_TICK:
                    break
                reason = f"Stigmergy: 发现 [{entry['knowledge_type']}] L{entry['level']} '{entry['title'][:80]}'"
                if self._active_stigmergy_spawn_exists(run_id, entry["id"], role, reason):
                    continue
                request_spawn(
                    self.db,
                    run_id=run_id,
                    requesting_agent="stigmergy",
                    requested_role=role,
                    reason=reason,
                    context_entry_ids=[entry["id"]],
                    priority=80,  # 自动 spawn 高优先级
                    commit=False,
                )
                spawned += 1

        if spawned:
            self.db.conn.commit()
            _log.info("StigmergySpawn: auto-generated %d spawn(s) for run=%s", spawned, run_id)
            self._log_behavior(run_id, "emergence",
                f"Stigmergy 自动 spawn: {spawned} 个 agent 已入队")

    def _auto_spawn_roles(self, entry: dict) -> list:
        """根据知识条目类型决定应该 spawn 什么角色的 agent。"""
        roles = set()
        ktype = entry.get("knowledge_type", "") if isinstance(entry, dict) else entry["knowledge_type"]
        level = entry.get("level", 1) if isinstance(entry, dict) else entry["level"]

        if ktype == "vulnerability":
            roles.add("analyst")
            if level >= 3:
                roles.add("exploiter")
        elif ktype == "observation" and level >= 3:
            roles.add("analyst")
        elif ktype == "pattern":
            roles.add("analyst")

        return list(roles)

    def _active_stigmergy_spawn_exists(
        self,
        run_id: str,
        entry_id: str,
        role: str,
        reason: str,
    ) -> bool:
        dedup_key = build_spawn_dedup_key(run_id, role, reason, [entry_id])
        row = self.db.fetch_one(
            """SELECT 1
               FROM spawn_requests
               WHERE run_id = ?
                 AND requested_role = ?
                 AND status IN ('pending', 'spawning')
                 AND (
                   dedup_key = ?
                   OR EXISTS (
                     SELECT 1
                     FROM json_each(spawn_requests.context_entry_ids)
                     WHERE json_each.value = ?
                   )
                 )
               LIMIT 1""",
            (run_id, role, dedup_key, entry_id),
        )
        return row is not None

    async def _tick_work_market(self, run_id: str):
        """
        维护共享任务市场。

        Agent 可以直接 claim_work_tasks() 抢任务；Orchestrator 负责恢复卡住的
        claim，并在某个角色没有足够 live agent 时留下 spawn 信号。
        """
        recovered = recover_stale_work_claims(self.db)
        if recovered:
            _log.info("WorkMarket: recovered %d stale task claims", recovered)

        pending = poll_work_tasks(self.db, run_id=run_id, status="pending", limit=50)
        if not pending:
            return

        live_agents = get_live_agents(self.db, run_id)
        idle_by_role: Dict[str, int] = {}
        for agent in live_agents:
            if (agent.get("load_score") or 0) < 0.5 and agent.get("stealable", 1):
                idle_by_role[agent["role"]] = idle_by_role.get(agent["role"], 0) + 1

        by_role: Dict[str, List[Dict[str, Any]]] = {}
        for task in pending:
            role = task.get("required_role") or self._role_for_task_type(task["task_type"])
            by_role.setdefault(role, []).append(task)

        for role, tasks in by_role.items():
            active_spawn = self.db.fetch_one(
                """SELECT COUNT(*) AS c FROM spawn_requests
                   WHERE run_id = ? AND requested_role = ?
                     AND status IN ('pending', 'spawning')""",
                (run_id, role),
            )["c"]
            capacity = idle_by_role.get(role, 0) + active_spawn
            if len(tasks) <= capacity:
                continue

            top = tasks[0]
            context_ids = self._task_context_ids(top)
            request_spawn(
                self.db,
                run_id=run_id,
                requesting_agent="work-market",
                requested_role=role,
                reason=f"任务市场积压: {role} 有 {len(tasks)} 个待处理任务",
                context_entry_ids=context_ids,
                parent_task_id=top["task_id"],
                priority=top["priority"] if top["priority"] is not None else 50,
                commit=False,
            )
            self.db.conn.commit()
            _log.info("WorkMarket: requested %s for %d pending tasks", role, len(tasks))
            self._log_behavior(
                run_id, "adaptation",
                f"任务市场扩容: {role} 积压 {len(tasks)} 个任务",
            )

    async def _tick_heartbeat(self, run_id: str):
        """清理僵尸 Agent"""
        stale = cleanup_stale_agents(self.db)
        if stale:
            _log.warning("Heartbeat: cleaned %d stale agents: %s", len(stale), [s[:8] for s in stale])
            self._log_behavior(
                run_id, "adaptation",
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

            result = run_promotion_cycle(self.db)
            promote_count = result.get("promoted", 0) if isinstance(result, dict) else result
            if promote_count:
                _log.info("Governance: promoted %s entries", promote_count)
                self._log_behavior(
                    run_id, "optimization",
                    f"DIKW 提升: {promote_count} 条知识升级",
                )

            decay_result = check_and_decay(self.db)
            decayed_total = (
                len(decay_result.get("decayed_rules", [])) +
                len(decay_result.get("decayed_entries", []))
                if isinstance(decay_result, dict) else int(decay_result or 0)
            )
            if decayed_total:
                _log.info("Governance: decayed %d stale items", decayed_total)

            # 信息素衰减
            phero_result = run_pheromone_decay(self.db)
            if phero_result.get("decayed") or phero_result.get("stale_marked"):
                _log.info("Governance: pheromone decayed %d, stale %d",
                          len(phero_result.get("decayed", [])),
                          len(phero_result.get("stale_marked", [])))

            # 策略自动蒸馏
            distill_result = auto_distill_strategies(self.db)
            if distill_result.get("distilled"):
                _log.info("Governance: auto-distilled %d strategies", len(distill_result["distilled"]))

            # 独立验证 pipeline
            auto_enqueue_validations(self.db, run_id)
            verify_result = process_validation_queue(self.db)
            if verify_result.get("processed", 0) > 0:
                _log.info("Governance: verified %d (confirmed=%d, refuted=%d)",
                          verify_result["processed"],
                          verify_result.get("confirmed", 0),
                          verify_result.get("refuted", 0))
                self._log_behavior(
                    run_id, "optimization",
                    f"独立验证: {verify_result['processed']}条 "
                    f"(确认={verify_result.get('confirmed',0)}, 反驳={verify_result.get('refuted',0)})",
                )

            # Wisdom 蒸馏 (L4 → distilled_rules)
            wisdom_result = distill_wisdom(self.db)
            if wisdom_result.get("distilled"):
                _log.info("Governance: distilled %d wisdom rules", len(wisdom_result["distilled"]))
                self._log_behavior(
                    run_id, "emergence",
                    f"Wisdom 蒸馏: {len(wisdom_result['distilled'])} 条规则",
                )

            # Ontology 关系自动发现
            onto_result = discover_relations_from_cooccurrence(self.db)
            if onto_result.get("discovered"):
                _log.info("Governance: discovered %d ontology relations",
                          len(onto_result["discovered"]))
                self._log_behavior(
                    run_id, "emergence",
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
                # 乐观锁：读取当前版本，CAS 更新
                for attempt in range(2):
                    row = self.db.fetch_one(
                        "SELECT strategy_version FROM swarm_runs WHERE run_id = ?",
                        (run_id,),
                    )
                    if not row:
                        break
                    current_ver = row["strategy_version"]
                    self.db.execute(
                        "UPDATE swarm_runs SET budget_strategy = ?, strategy_version = strategy_version + 1 WHERE run_id = ? AND strategy_version = ?",
                        (new_strategy, run_id, current_ver),
                    )
                    if self.db.conn.total_changes > 0:
                        self.db.conn.commit()
                        break
                    _log.debug("PowerSchedule: budget_strategy CAS conflict, retry %d/2", attempt + 1)
                else:
                    # 降级：无锁更新
                    self.db.execute(
                        "UPDATE swarm_runs SET budget_strategy = ?, strategy_version = strategy_version + 1 WHERE run_id = ?",
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

    async def _tick_controller(self, run_id: str):
        """
        Controller LLM 判决 — Phase B.
        每 POLL_CONTROLLER_SEC 审视所有 Worker 信号，
        调用 LLM (或降级规则) 做 kill/boost/spawn 决策。
        """
        try:
            from .controller import Controller
            ctrl = Controller(self.db, mode="llm")  # LLM 驱动 — 架构设计意图；LLM 失败时自动降级 rules
            decisions = await ctrl.tick(run_id)
            if decisions:
                _log.info("Controller: %d decisions for run=%s", len(decisions), run_id)
                for d in decisions:
                    self._log_behavior(run_id, "adaptation",
                        f"Controller → {d.decision_type} {d.target_agent_id or d.target_role}: {d.reason[:80]}")
        except Exception as e:
            _log.warning("Controller: tick failed: %s", e)

    async def _balance_load(self, run_id: str):
        """
        工作窃取式负载均衡。
        
        读取 agent_heartbeats.load_score:
        - load_score < 0.3 的 agent 标记为可窃取
        - 按角色从共享任务市场原子 claim work
        """
        live_agents = get_live_agents(self.db, run_id)
        if not live_agents:
            return

        # 找空闲 agent (load < 0.3, stealable=1)
        idle_agents = [
            a for a in live_agents
            if (a.get("load_score") or 0) < 0.3 and a.get("stealable", 1)
        ]
        if not idle_agents:
            return

        assigned = 0
        for agent in idle_agents:
            claimed = claim_work_tasks(
                self.db,
                run_id=run_id,
                agent_id=agent["agent_id"],
                role=agent["role"],
                limit=1,
            )
            if not claimed:
                continue
            task_id = claimed[0]["task_id"]
            self.db.execute(
                """UPDATE agent_heartbeats
                   SET current_task_id = ?, load_score = MAX(load_score, 0.5)
                   WHERE agent_id = ?""",
                (task_id, agent["agent_id"]),
            )
            self.db.conn.commit()
            assigned += 1

        if assigned:
            _log.info("LoadBalance: claimed %d market tasks for idle agents", assigned)
            self._log_behavior(run_id, "optimization",
                f"负载均衡: {assigned} 个空闲 agent 从任务市场领取工作")

    def _role_for_task_type(self, task_type: str) -> str:
        return {
            "scan": "scanner",
            "analyze": "analyst",
            "exploit": "exploiter",
            "report": "reporter",
        }.get(task_type, "custom")

    def _task_context_ids(self, task: dict) -> List[str]:
        try:
            focus = json.loads(task.get("focus_params") or "{}")
        except (json.JSONDecodeError, TypeError):
            return []
        ids = focus.get("context_entry_ids", [])
        return ids if isinstance(ids, list) else []

    def _build_spawn_context(self, req: dict) -> str:
        """
        为新 Agent 构建 KB 上下文注入。
        从触发 spawn 的知识条目中提取相关信息。

        Phase A: 注入蜂群探索记忆，让 Agent 知道哪些路径已被测试过。
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

        # Phase A: 注入蜂群探索记忆
        try:
            run_id = req.get("run_id", "")
            if run_id:
                from src.swarm.exploration import build_exploration_context
                exploration_ctx = build_exploration_context(self.db, run_id)
                if exploration_ctx:
                    parts.append("\n" + exploration_ctx)
                    parts.append(
                        "\n> ⚠️ 以上是蜂群已探索的路径记录。你可以用它避免重复已测试的组合，"
                        "但注意：如果之前的测试深度是 'shallow'，你可能需要用更深的技术重新测试。"
                        "记录显示 'not_found' ≠ 一定没有漏洞。自行判断是否需要重新探索。"
                    )
        except Exception:
            pass

        return "\n".join(parts)

    def _build_spawn_context_worker(self, req: dict) -> str:
        """
        Worker 专用上下文 — 只给任务，不给全局探索记忆。
        Worker 不知道自己被 Controller 监控。
        """
        context_entry_ids = req.get("context_entry_ids", "[]")
        try:
            entry_ids = json.loads(context_entry_ids) if isinstance(context_entry_ids, str) else context_entry_ids
        except (json.JSONDecodeError, TypeError):
            entry_ids = []

        parts = [f"## 任务\n{req.get('reason', req['reason'])}"]

        # 只注入触发知识条目的摘要
        if entry_ids:
            parts.append("\n## 关联发现")
            for eid in entry_ids[:2]:
                row = self.db.fetch_one(
                    "SELECT title, knowledge_type, level FROM knowledge_entries WHERE id = ?",
                    (eid,),
                )
                if row:
                    parts.append(f"- [{row['knowledge_type']}] L{row['level']}: {row['title'][:100]}")

        return "\n".join(parts)

    async def _tick_health(self, run_id: str):
        """健康检查: 验证关键子模块可用，Controller 活跃，表不溢出。"""
        issues = []

        # Check sub-module importability
        for mod_name in ("src.swarm.signals", "src.swarm.controller",
                         "src.swarm.exploration", "src.governance.engine"):
            try:
                import importlib
                importlib.import_module(mod_name.replace("/", "."))
            except Exception as e:
                issues.append(f"import_failed:{mod_name}:{e}")

        # Check controller last success
        try:
            last_decision = self.db.fetch_one(
                "SELECT MAX(created_at) as last_ts FROM controller_decisions"
            )
            if last_decision and last_decision["last_ts"]:
                _log.debug("HealthCheck: last controller decision at %s", last_decision["last_ts"])
        except Exception as e:
            issues.append(f"controller_audit_query_failed:{e}")

        # Check worker_signals table size
        try:
            count = self.db.fetch_one("SELECT COUNT(*) as c FROM worker_signals")
            if count and count["c"] > 100000:
                issues.append(f"worker_signals_table_large:{count['c']} rows")
        except Exception as e:
            issues.append(f"worker_signals_count_failed:{e}")

        if issues:
            for issue in issues:
                _log.warning("HealthCheck: %s", issue)
            # Log via regular behavior table with valid type
            self._log_behavior(run_id, "optimization",
                f"Health issues: {'; '.join(issues)}")
        else:
            _log.debug("HealthCheck: all ok for run=%s", run_id)

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
