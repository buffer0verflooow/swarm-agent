"""
Controller — Opus-level LLM 判决层 (Controller/Worker Phase B)

每 60s 审视所有 Worker 的信号，做出 kill/boost/spawn/redirect 决策。

两种模式:
  - llm: 调用 Opus 级 LLM (Zenmux/GLM-5.2) 做智能判决
  - rules: 纯规则降级模式 (LLM 不可用时自动切换)

LLM 通信:
  - 读取 ~/.hermes/config.yaml 中的 custom_providers 获取 API key
  - 直接 HTTP POST 到 OpenAI-compatible /v1/chat/completions
  - 不依赖 delegate_task (Orchestrator 进程内独立运行)

用法:
    from src.swarm.controller import Controller, controller_tick

    ctrl = Controller(db)
    decisions = await ctrl.tick(run_id)

    # 或简化版
    decisions = await controller_tick(db, run_id)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_log = logging.getLogger("swarm_knowledge.controller")

# ── 常量 ──

DEFAULT_LLM_MODEL = "z-ai/glm-5.2"
DEFAULT_LLM_PROVIDER = "custom:zenmux.ai"
DEFAULT_BASE_URL = "https://zenmux.ai/api/v1"
MAX_DECISIONS_PER_TICK = 3       # 每次 tick 最多做 3 个决策
CONTROLLER_TICK_SEC = 60          # controller tick 间隔
CONTROLLER_TEMPERATURE = 0.3      # 低温度 → 更确定性的决策

# 规则模式的阈值
RULE_KILL_QUALITY = 0.25
RULE_KILL_STUCK_MINUTES = 3       # 连续 N 分钟无有用输出
RULE_BOOST_QUALITY = 0.70
RULE_BOOST_NOVELTY = 0.30   # boost 对 novelty 要求放宽 (高质量本身已足够)
RULE_SPAWN_WHEN_FEWER_THAN = 2    # 活跃 Worker 低于此数 → spawn


# ── Data Classes ──

class ControllerDecision:
    __slots__ = ("decision_type", "target_agent_id", "target_role",
                 "reason", "confidence", "metadata")
    def __init__(self, decision_type: str, target_agent_id: str = "",
                 target_role: str = "", reason: str = "",
                 confidence: float = 0.7, metadata: Dict = None):
        self.decision_type = decision_type
        self.target_agent_id = target_agent_id
        self.target_role = target_role
        self.reason = reason
        self.confidence = confidence
        self.metadata = metadata or {}


class Controller:
    """LLM 驱动的蜂群控制器。

    llm_fn: 可选的 LLM 调用函数，签名 async fn(prompt: str) -> str
            如果不提供，自动从环境配置中读取 Zenmux/GLM-5.2 端点。
    """

    def __init__(self, db, llm_fn: Callable = None, mode: str = "llm"):
        self.db = db
        self.llm_fn = llm_fn
        self.mode = mode           # "llm" or "rules"
        self.tick_number = 0
        self._api_key = None
        self._base_url = None
        self._model = None

    async def tick(self, run_id: str) -> List[ControllerDecision]:
        """一次判决周期。"""
        self.tick_number += 1

        # 收集输入
        worker_summary = self._gather_worker_summary(run_id)
        global_state = self._gather_global_state(run_id)

        if not worker_summary:
            _log.debug("Controller: no workers to review for run=%s", run_id)
            return []

        # 判决
        try:
            if self.mode == "llm":
                decisions = await self._tick_llm(run_id, worker_summary, global_state)
            else:
                decisions = self._tick_rules(run_id, worker_summary, global_state)
        except Exception as e:
            _log.warning("Controller: LLM call failed, falling back to rules: %s", e)
            decisions = self._tick_rules(run_id, worker_summary, global_state)

        # 执行 + 记录
        for d in decisions:
            self._execute_decision(run_id, d)
            self._record_decision(run_id, d)

        return decisions

    # ── Gather ──

    def _gather_worker_summary(self, run_id: str) -> List[Dict]:
        from .signals import get_all_worker_signals, detect_loops
        summary = get_all_worker_signals(self.db, run_id, window_seconds=300)
        for s in summary:
            is_stuck, reason = detect_loops(self.db, s["agent_id"], run_id)
            s["loop_detected"] = 1 if is_stuck else 0
            s["loop_reason"] = reason if is_stuck else ""
        return summary

    def _gather_global_state(self, run_id: str) -> Dict:
        run = self.db.fetch_one(
            "SELECT token_budget, tokens_spent, budget_strategy FROM swarm_runs WHERE run_id = ?",
            (run_id,),
        )
        budget = run["token_budget"] or 100000 if run else 100000
        spent = run["tokens_spent"] or 0 if run else 0

        from .exploration import get_exploration_summary
        exp = get_exploration_summary(self.db, run_id)

        from .lifecycle import get_live_agents
        live = len(get_live_agents(self.db, run_id))

        return {
            "budget_total": budget,
            "budget_spent": spent,
            "budget_ratio": spent / max(1, budget),
            "budget_strategy": run["budget_strategy"] if run else "balanced",
            "active_workers": live,
            "explored_targets": exp.get("unique_targets", 0),
            "explored_combos": exp.get("unique_coverage", 0),
            "total_traces": exp.get("total_traces", 0),
        }

    # ── LLM Mode ──

    async def _tick_llm(self, run_id: str, workers: List[Dict],
                         state: Dict) -> List[ControllerDecision]:
        prompt = self._build_llm_prompt(workers, state)
        response = await self._call_llm(prompt)
        return self._parse_llm_response(response, workers)

    def _build_llm_prompt(self, workers: List[Dict], state: Dict) -> str:
        """构建 Controller 的 LLM prompt (caveman 风格 + 结构化)。"""
        lines = [
            "你是蜂群控制器。审视 Worker 状态，输出 kill/boost/spawn/redirect/noop 决策。",
            "",
            "## 全局状态",
            f"- budget: {state['budget_spent']}/{state['budget_total']} "
            f"({state['budget_ratio']:.0%} spent, strategy={state['budget_strategy']})",
            f"- active workers: {state['active_workers']}",
            f"- explored: {state['explored_targets']} targets, "
            f"{state['explored_combos']} combos ({state['total_traces']} traces)",
            "",
            "## Worker 状态",
        ]

        if workers:
            lines.append("| # | agent | quality | novelty | efficiency | stuck | loops? | progress |")
            lines.append("|---|-------|---------|---------|------------|-------|--------|----------|")
            for i, w in enumerate(workers):
                stuck_flag = "YES" if (w.get("loop_detected") or w.get("is_stuck")) else "no"
                aid = w["agent_id"][:12]
                lines.append(
                    f"| {i+1} | {aid} | {w['avg_quality']:.2f} | "
                    f"{w['avg_novelty']:.2f} | {w['avg_efficiency']:.3f} | "
                    f"{stuck_flag} | {w.get('loop_reason','')[:40]} | "
                    f"{w.get('latest_progress','-')[:30]} |"
                )
        else:
            lines.append("(no active workers)")

        lines.extend([
            "",
            "## 可用动作",
            "- kill <agent_id>: 终止无用的 Worker，释放资源",
            "- boost <agent_id>: 增加该 Worker 的 budget 权重",
            "- spawn <role>: 生成新的 Worker（scanner/analyst/exploiter/reporter）",
            "- redirect <agent_id> <new_target>: 让 Worker 换方向",
            "- noop: 一切正常，不做任何改动",
            "",
            "## 决策规则",
            "- quality<0.25 + stuck → kill",
            "- quality<0.15 → kill (无论 stuck)",
            "- quality>0.7 + novelty>0.5 → boost",
            "- 连续 3 条 novelty<0.1 → kill (兜圈)",
            "- active_workers < 2 → spawn scanner",
            "- budget>70% spent + 无 HIGH 漏洞 → spawn analyst 深挖",
            "- 同时有 stuck worker 和 < 2 个活跃 → kill + spawn pair",
            "",
            "输出 JSON 数组 (最多 3 个决策):",
            '[{"act":"kill","agent":"<id>","because":"<reason>"},'
            '{"act":"spawn","role":"scanner","because":"<reason>"}]',
            "",
            "只输出 JSON。不要解释。",
        ])

        return "\n".join(lines)

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM API。"""
        if self.llm_fn:
            result = self.llm_fn(prompt)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        # Fallback: 直接调 HTTP API
        api_key = self._get_api_key()
        base_url = self._get_base_url()
        model = self._get_model()

        import urllib.request
        import urllib.error

        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": CONTROLLER_TEMPERATURE,
        }).encode()

        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                content = data["choices"][0]["message"]["content"]
                _log.info("Controller LLM: %d tokens", data.get("usage", {}).get("total_tokens", 0))
                return content
        except urllib.error.HTTPError as e:
            _log.error("Controller LLM HTTP %d: %s", e.code, e.read()[:200])
            raise
        except Exception as e:
            _log.error("Controller LLM call failed: %s", e)
            raise

    def _parse_llm_response(self, response: str,
                            workers: List[Dict]) -> List[ControllerDecision]:
        """从 LLM 原始输出解析决策 JSON。"""
        # 提取 JSON 数组
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if not json_match:
            _log.warning("Controller: no JSON found in LLM response: %s", response[:200])
            return []

        try:
            raw = json.loads(json_match.group())
        except json.JSONDecodeError:
            _log.warning("Controller: invalid JSON from LLM: %s", response[:200])
            return []

        decisions = []
        worker_ids = {w["agent_id"] for w in workers}

        for item in raw[:MAX_DECISIONS_PER_TICK]:
            act = item.get("act", "")
            if act == "kill":
                aid = item.get("agent", "")
                if aid and aid in worker_ids:
                    decisions.append(ControllerDecision(
                        "kill", target_agent_id=aid,
                        reason=item.get("because", "LLM decision"),
                    ))
            elif act == "boost":
                aid = item.get("agent", "")
                if aid and aid in worker_ids:
                    decisions.append(ControllerDecision(
                        "boost", target_agent_id=aid,
                        reason=item.get("because", "LLM decision"),
                    ))
            elif act == "spawn":
                role = item.get("role", "scanner")
                if role in ("scanner", "analyst", "exploiter", "reporter"):
                    decisions.append(ControllerDecision(
                        "spawn", target_role=role,
                        reason=item.get("because", "LLM decision"),
                    ))
            elif act == "redirect":
                decisions.append(ControllerDecision(
                    "redirect",
                    target_agent_id=item.get("agent", ""),
                    reason=item.get("because", "LLM decision"),
                    metadata={"new_target": item.get("target", "")},
                ))
            elif act == "adjust_budget":
                decisions.append(ControllerDecision(
                    "adjust_budget",
                    reason=item.get("because", "LLM decision"),
                    metadata={"strategy": item.get("strategy", "balanced")},
                ))

        return decisions

    # ── Rules Mode (降级) ──

    def _tick_rules(self, run_id: str, workers: List[Dict],
                    state: Dict) -> List[ControllerDecision]:
        """纯规则降级模式。当 LLM 不可用时使用。"""
        decisions = []

        # Stuck/dead workers → kill
        for w in workers:
            if w.get("loop_detected") or w.get("is_stuck"):
                decisions.append(ControllerDecision(
                    "kill", target_agent_id=w["agent_id"],
                    reason=f"stuck: quality={w['avg_quality']:.2f}",
                    confidence=0.85,
                ))
            elif w["avg_quality"] < RULE_KILL_QUALITY and w["signal_count"] > 3:
                decisions.append(ControllerDecision(
                    "kill", target_agent_id=w["agent_id"],
                    reason=f"low quality: {w['avg_quality']:.2f} < {RULE_KILL_QUALITY}",
                    confidence=0.70,
                ))

        # High performers → boost (但不和 kill 同一个 agent)
        killed = {d.target_agent_id for d in decisions}
        for w in workers:
            if w["agent_id"] in killed:
                continue
            if w["avg_quality"] > RULE_BOOST_QUALITY and w["avg_novelty"] > RULE_BOOST_NOVELTY:
                decisions.append(ControllerDecision(
                    "boost", target_agent_id=w["agent_id"],
                    reason=f"high performer: quality={w['avg_quality']:.2f}",
                    confidence=0.75,
                ))

        # Too few workers → spawn
        alive_after_kill = state["active_workers"] - len([d for d in decisions if d.decision_type == "kill"])
        if alive_after_kill < RULE_SPAWN_WHEN_FEWER_THAN:
            decisions.append(ControllerDecision(
                "spawn", target_role="scanner",
                reason=f"only {alive_after_kill} workers left after kills, need more",
                confidence=0.80,
            ))

        # Budget nearly exhausted + no HIGH findings → spawn analyst to focus
        if state["budget_ratio"] > 0.7:
            decisions.append(ControllerDecision(
                "adjust_budget",
                reason=f"budget {state['budget_ratio']:.0%} → switch depth",
                metadata={"strategy": "depth"},
                confidence=0.60,
            ))

        # Cap the execution decisions, but always include budget adjustments
        exec_decisions = decisions[:MAX_DECISIONS_PER_TICK]
        budget_decisions = [d for d in decisions if d.decision_type == "adjust_budget"]
        non_budget = [d for d in exec_decisions if d.decision_type != "adjust_budget"]
        return non_budget + budget_decisions[:1]  # at most 1 budget decision per tick

    # ── Execute ──

    def _execute_decision(self, run_id: str, d: ControllerDecision):
        """执行一个决策。"""
        try:
            if d.decision_type == "kill":
                self._execute_kill(run_id, d)
            elif d.decision_type == "boost":
                self._execute_boost(run_id, d)
            elif d.decision_type == "spawn":
                self._execute_spawn(run_id, d)
            elif d.decision_type == "adjust_budget":
                self._execute_adjust_budget(run_id, d)
            elif d.decision_type == "redirect":
                self._execute_redirect(run_id, d)
            d.metadata["executed"] = True
        except Exception as e:
            _log.error("Controller: execute failed for %s: %s", d.decision_type, e)
            d.metadata["executed"] = False
            d.metadata["error"] = str(e)

    def _execute_kill(self, run_id: str, d: ControllerDecision):
        """Kill Worker: 标记 agent_profiles 为 deprecated，取消 pending spawn。"""
        aid = d.target_agent_id
        self.db.execute(
            "UPDATE agent_profiles SET status = 'deprecated', updated_at = datetime('now') WHERE agent_id = ?",
            (aid,),
        )
        self.db.execute(
            "DELETE FROM agent_heartbeats WHERE agent_id = ?", (aid,),
        )
        # 拒绝该 agent 的所有 pending spawn 请求
        self.db.execute(
            """UPDATE spawn_requests SET status = 'rejected',
               reason = reason || ' [killed by controller]'
               WHERE spawned_agent_id = ? AND status IN ('pending','spawning')""",
            (aid,),
        )
        # 释放该 agent 的 running tasks → pending
        self.db.execute(
            """UPDATE agent_tasks SET status = 'pending', agent_id = NULL, claimed_at = NULL
               WHERE agent_id = ? AND status = 'running'""",
            (aid,),
        )
        self.db.conn.commit()
        _log.info("Controller: killed %s — %s", aid[:12], d.reason[:60])

    def _execute_boost(self, run_id: str, d: ControllerDecision):
        """Boost Worker: 提升其 pending task 的优先级。"""
        aid = d.target_agent_id
        self.db.execute(
            """UPDATE agent_tasks SET priority = MIN(100, priority + 20)
               WHERE agent_id = ? AND status IN ('pending','running')""",
            (aid,),
        )
        self.db.conn.commit()
        _log.info("Controller: boosted %s — %s", aid[:12], d.reason[:60])

    def _execute_spawn(self, run_id: str, d: ControllerDecision):
        """Spawn new Worker: 通过 spawn_requests 信号 (worker_mode=true)。"""
        from .spawner import request_spawn
        request_spawn(
            self.db,
            run_id=run_id,
            requesting_agent="controller",
            requested_role=d.target_role,
            reason=f"Controller auto-spawn: {d.reason}",
            priority=70,
        )
        # Mark as worker mode spawn via metadata
        self.db.execute(
            "UPDATE spawn_requests SET reason = reason || ' [worker_mode]' WHERE run_id = ? AND requesting_agent = 'controller' AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
        self.db.conn.commit()
        _log.info("Controller: spawn %s — %s", d.target_role, d.reason[:60])

    def _execute_adjust_budget(self, run_id: str, d: ControllerDecision):
        """调整全局 budget strategy。"""
        strategy = d.metadata.get("strategy", "balanced")
        self.db.execute(
            "UPDATE swarm_runs SET budget_strategy = ? WHERE run_id = ?",
            (strategy, run_id),
        )
        self.db.conn.commit()
        _log.info("Controller: budget strategy → %s", strategy)

    def _execute_redirect(self, run_id: str, d: ControllerDecision):
        """Redirect Worker: 更新其 task focus_params（以注入新方向）。"""
        aid = d.target_agent_id
        new_target = d.metadata.get("new_target", "")
        if new_target:
            self.db.execute(
                """UPDATE agent_tasks SET focus_params = json_set(
                       COALESCE(focus_params, '{}'), '$.redirected_to', ?)
                   WHERE agent_id = ? AND status = 'running'""",
                (new_target, aid),
            )
            self.db.conn.commit()
        _log.info("Controller: redirected %s → %s", aid[:12], new_target)

    # ── Audit ──

    def _record_decision(self, run_id: str, d: ControllerDecision):
        """记录决策到审计表。"""
        decision_id = str(uuid.uuid4())
        self.db.execute(
            """INSERT INTO controller_decisions
               (decision_id, run_id, tick_number, decision_type,
                target_agent_id, target_role, reason, confidence,
                status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'executed', datetime('now'))""",
            (
                decision_id, run_id, self.tick_number,
                d.decision_type, d.target_agent_id, d.target_role,
                d.reason, d.confidence,
            ),
        )
        self.db.conn.commit()

    # ── API Key Resolution ──

    def _get_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        # 尝试从 Hermes config 读取
        config_path = Path.home() / ".hermes" / "config.yaml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                for prov in cfg.get("custom_providers", []):
                    if "zenmux" in prov.get("name", "").lower():
                        self._api_key = prov.get("api_key", "")
                        self._base_url = prov.get("base_url", DEFAULT_BASE_URL)
                        self._model = prov.get("model", DEFAULT_LLM_MODEL)
                        break
            except Exception:
                pass
        if not self._api_key:
            self._api_key = os.environ.get("ZENMUX_API_KEY", "")
        return self._api_key or ""

    def _get_base_url(self) -> str:
        if not self._base_url:
            self._get_api_key()  # triggers config load
        return self._base_url or DEFAULT_BASE_URL

    def _get_model(self) -> str:
        if not self._model:
            self._get_api_key()
        return self._model or DEFAULT_LLM_MODEL


# ── 便捷入口 ──

async def controller_tick(db, run_id: str,
                          mode: str = "llm",
                          llm_fn: Callable = None) -> List[ControllerDecision]:
    """Controller 便捷入口。"""
    ctrl = Controller(db, llm_fn=llm_fn, mode=mode)
    return await ctrl.tick(run_id)
