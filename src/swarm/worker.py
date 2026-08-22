"""
Swarm worker loop.

A worker is the runtime side of the work market: it registers an agent, claims
role-matching tasks, builds KB context, runs an executor, captures the result,
and marks the task complete or failed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .lifecycle import AgentLifecycle
from .model_config import (
    assign_task_model_profile,
    record_model_usage,
    record_swarm_event,
    resolve_execution_model,
    resolve_task_model_profile,
)
from .artifacts import verify_artifacts
from .safety import mark_untrusted, sanitize_single_line
from .work_queue import claim_work_tasks, complete_work_task, fail_work_task
from ..agents.capture import CaptureContext, CaptureSource, capture

_log = logging.getLogger("swarm_knowledge.worker")

Executor = Callable[[Dict[str, Any], str], Any]

# 角色终止 label 集合（DeepTutor LabelProtocol 移植）— controller/runner 只认
# label 不解析内容，worker 显式声明"我为什么停"：
#   DONE      — 正常完成任务
#   BLOCKED   — 遇到不可逾越的障碍（WAF/限速/授权缺失），声明失败
#   EXHAUSTED — 资源/路径穷尽，没有更多可做
TERMINAL_LABELS: dict[str, set[str]] = {
    "scanner": {"DONE", "BLOCKED", "EXHAUSTED"},
    "analyst": {"DONE", "BLOCKED", "EXHAUSTED"},
    "exploiter": {"DONE", "BLOCKED", "EXHAUSTED"},
    "reporter": {"DONE", "BLOCKED"},
}
# 兜底角色（custom 等）
DEFAULT_TERMINAL_LABELS = {"DONE", "BLOCKED", "EXHAUSTED"}


@dataclass
class WorkerResult:
    task_id: str
    status: str
    captured_entry_id: Optional[str] = None
    error: str = ""
    final_label: str = ""


def _loads_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _normalize_artifacts(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (str, dict)):
        return [value]
    return []


def normalize_executor_result(result: Any) -> Dict[str, Any]:
    """
    Normalize executor output.

    Supported forms:
    - string: captured as content
    - dict: may include content/output/summary, metadata, tags, token_cost,
      success, capture
    """
    if isinstance(result, dict):
        content = (
            result.get("content")
            or result.get("output")
            or result.get("summary")
            or result.get("result")
            or ""
        )
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        return {
            "success": bool(result.get("success", True)),
            "capture": bool(result.get("capture", True)),
            "content": content,
            "metadata": metadata,
            "tags": result.get("tags", []),
            "artifacts": _normalize_artifacts(result.get("artifacts")),
            "title": result.get("title", ""),
            "intent": result.get("intent", ""),
            "token_cost": int(result.get("token_cost") or 0),
            "result_summary": result.get("result_summary"),
            "error": str(result.get("error", "")),
            "final_label": str(result.get("final_label", "")),
        }

    content = "" if result is None else str(result)
    return {
        "success": True,
        "capture": True,
        "content": content,
        "metadata": {},
        "tags": [],
        "artifacts": [],
        "title": "",
        "intent": "",
        "token_cost": 0,
        "result_summary": None,
        "error": "",
        "final_label": "",
    }


class SwarmWorker:
    """Role worker that continuously claims tasks from the shared market."""

    def __init__(
        self,
        db,
        run_id: str,
        agent_id: str,
        role: str,
        executor: Optional[Executor] = None,
        poll_interval: float = 2.0,
        claim_limit: int = 1,
        model_profile_id: Optional[str] = None,
    ):
        self.db = db
        self.run_id = run_id
        self.agent_id = agent_id
        self.role = role
        self.executor = executor
        self.poll_interval = max(0.1, float(poll_interval))
        self.claim_limit = max(1, int(claim_limit))
        self.model_profile_id = model_profile_id
        self.lifecycle = AgentLifecycle(db, agent_id, run_id)
        self._registered = False
        self._stopped = False

    def register(self, capabilities: Optional[List[str]] = None) -> None:
        self.lifecycle.register(
            role=self.role,
            capabilities=capabilities or [],
            model_profile_id=self.model_profile_id,
        )
        self._registered = True

    def stop(self) -> None:
        self._stopped = True

    def is_killed(self) -> bool:
        """True if the controller has killed this agent (agent_profiles → deprecated).

        The controller writes its kill decision only to the DB; the worker
        process must poll this itself to actually stop claiming/executing.
        """
        try:
            row = self.db.fetch_one(
                "SELECT status FROM agent_profiles WHERE agent_id = ?", (self.agent_id,)
            )
        except Exception:
            return False
        return bool(row and row["status"] == "deprecated")

    def claim_once(self) -> Optional[Dict[str, Any]]:
        """Claim one task and return it with built context, without executing."""
        # Controller-kill check MUST precede _ensure_registered: register()'s
        # UPSERT would otherwise flip agent_profiles back to 'active',
        # resurrecting a killed worker.
        if self.is_killed():
            self._stopped = True
            _log.info("worker %s killed by controller; skipping claim", self.agent_id)
            return None
        self._ensure_registered()
        claimed = claim_work_tasks(
            self.db,
            run_id=self.run_id,
            agent_id=self.agent_id,
            role=self.role,
            limit=1,
        )
        if not claimed:
            self.lifecycle.beat(current_task_id=None, load=0.0)
            return None

        task = claimed[0]
        original_profile_id = task.get("model_profile_id")
        # 模型对照表入口 (migration 020): 免费池轮询 → 超限降级付费。
        # 返回附加 tier/engine/resolved_model 路由字段; 无 free 池时
        # 语义与旧 resolve_task_model_profile 一致。
        model_profile = resolve_execution_model(self.db, task) or resolve_task_model_profile(self.db, task)
        if model_profile:
            task["model_profile_id"] = model_profile["profile_id"]
            task["model_profile"] = model_profile
            if not original_profile_id:
                assign_task_model_profile(
                    self.db,
                    task["task_id"],
                    task.get("required_role") or self.role,
                    model_profile["profile_id"],
                )
            self.lifecycle.set_model_profile(
                model_profile_id=model_profile["profile_id"],
                model=f"{model_profile['provider']}:{model_profile['model']}",
            )
        context = build_task_context(self.db, task)
        record_swarm_event(
            self.db,
            run_id=self.run_id,
            event_type="task_claimed",
            source="swarm_worker",
            agent_id=self.agent_id,
            task_id=task["task_id"],
            content=f"{self.agent_id} claimed {task.get('required_role') or self.role}/{task.get('task_type')}",
            metadata={"model_profile": model_profile or {}},
        )
        self.lifecycle.beat(current_task_id=task["task_id"], load=0.8)
        return {"task": task, "context": context, "model_profile": model_profile}

    async def run_once(self) -> Optional[WorkerResult]:
        """Claim, execute, capture, and complete one task."""
        if self.executor is None:
            raise RuntimeError("SwarmWorker.run_once requires an executor")

        claimed = self.claim_once()
        if not claimed:
            return None

        task = claimed["task"]
        context = claimed["context"]
        task_id = task["task_id"]

        try:
            raw = self.executor(task, context)
            if inspect.isawaitable(raw):
                raw = await raw
            normalized = normalize_executor_result(raw)

            # 免费池用量记账 (migration 020): 免费模型被调用即计,
            # 失败也消耗免费额度 (token 尽力上报, 无则只计 calls)。
            profile_meta = task.get("model_profile") or {}
            if profile_meta.get("tier") == "free":
                record_model_usage(
                    self.db,
                    model_key=profile_meta.get("resolved_model")
                    or profile_meta.get("model")
                    or f"{profile_meta.get('provider')}/{profile_meta.get('model')}",
                    tokens=normalized["token_cost"],
                    calls=1,
                )

            # Killed while executing: controller already released this task back
            # to pending (agent_id=NULL); another worker may have re-claimed it.
            # Do not complete/fail anything — guarded UPDATEs below would match
            # the re-claimer's row without this early exit and poison its result.
            if self.is_killed():
                self._stopped = True
                _log.info(
                    "worker %s killed while running task %s; abandoning result",
                    self.agent_id, task_id,
                )
                self.lifecycle.beat(current_task_id=None, load=0.0)
                return WorkerResult(task_id=task_id, status="abandoned",
                                    error="killed by controller mid-task")

            if not normalized["success"]:
                error = normalized["error"] or "executor reported failure"
                fail_work_task(self.db, task_id, error, agent_id=self.agent_id)
                self.lifecycle.beat(current_task_id=None, load=0.0)
                return WorkerResult(task_id=task_id, status="failed", error=error)

            # 终止 label 声明（DeepTutor LabelProtocol 移植）— 在 artifact
            # verification 之前处理：label 是 worker 对"我为什么停"的结构化声明
            final_label = normalized.get("final_label", "") or ""
            if final_label:
                terminal = TERMINAL_LABELS.get(self.role, DEFAULT_TERMINAL_LABELS)
                if final_label not in terminal:
                    _log.warning(
                        "worker %s declared label %r not in terminal set for role=%s, ignoring",
                        self.agent_id,
                        final_label,
                        self.role,
                    )
                    final_label = ""

            if final_label == "BLOCKED":
                error = (
                    normalized["error"]
                    or f"worker declared BLOCKED: {normalized['content'][:200]}"
                )
                fail_work_task(self.db, task_id, error, agent_id=self.agent_id)
                record_swarm_event(
                    self.db,
                    run_id=self.run_id,
                    event_type="worker_blocked",
                    source="swarm_worker",
                    agent_id=self.agent_id,
                    task_id=task_id,
                    content=error,
                    metadata={"final_label": "BLOCKED"},
                )
                self.lifecycle.beat(current_task_id=None, load=0.0)
                return WorkerResult(
                    task_id=task_id, status="failed", error=error, final_label="BLOCKED"
                )

            artifact_verification = {"ok": True, "artifacts": [], "verified": [], "failed": []}
            if normalized["artifacts"]:
                artifact_verification = verify_artifacts(
                    self.db,
                    run_id=self.run_id,
                    task_id=task_id,
                    agent_id=self.agent_id,
                    artifacts=normalized["artifacts"],
                )
                if not artifact_verification["ok"]:
                    error = "artifact verification failed"
                    fail_work_task(self.db, task_id, error, agent_id=self.agent_id)
                    record_swarm_event(
                        self.db,
                        run_id=self.run_id,
                        event_type="artifact_verification_failed",
                        source="swarm_worker",
                        agent_id=self.agent_id,
                        task_id=task_id,
                        content=error,
                        metadata={"artifacts": artifact_verification["artifacts"]},
                    )
                    self.lifecycle.beat(current_task_id=None, load=0.0)
                    return WorkerResult(task_id=task_id, status="failed", error=error)

            captured_entry_id = None
            if normalized["capture"] and normalized["content"].strip():
                captured_entry_id = self._capture_task_result(task, normalized)

            summary = normalized["result_summary"] or {
                "content": normalized["content"][:500],
                "captured_entry_id": captured_entry_id,
                "worker_agent": self.agent_id,
                "worker_role": self.role,
                "model_profile": task.get("model_profile") or {},
                "artifact_verification": artifact_verification,
            }
            if final_label:
                summary["final_label"] = final_label
            complete_work_task(
                self.db,
                task_id,
                result_summary=summary,
                token_cost=normalized["token_cost"],
                agent_id=self.agent_id,
            )
            event_type = {
                "EXHAUSTED": "worker_exhausted",
                "DONE": "worker_done",
            }.get(final_label, "task_completed")
            record_swarm_event(
                self.db,
                run_id=self.run_id,
                event_type=event_type,
                source="swarm_worker",
                agent_id=self.agent_id,
                task_id=task_id,
                content=(normalized["content"] or "")[:1000],
                metadata={
                    "captured_entry_id": captured_entry_id,
                    "model_profile": task.get("model_profile") or {},
                    "token_cost": normalized["token_cost"],
                    "artifact_verification": artifact_verification,
                    **({"final_label": final_label} if final_label else {}),
                },
            )
            self.lifecycle.beat(current_task_id=None, load=0.0)
            return WorkerResult(
                task_id=task_id,
                status="completed",
                captured_entry_id=captured_entry_id,
                final_label=final_label,
            )
        except Exception as exc:
            fail_work_task(self.db, task_id, str(exc), agent_id=self.agent_id)
            self.lifecycle.beat(current_task_id=None, load=0.0)
            _log.exception("worker task failed: task_id=%s", task_id)
            return WorkerResult(task_id=task_id, status="failed", error=str(exc))

    async def run_loop(self, max_tasks: Optional[int] = None) -> Dict[str, int]:
        """Run until stopped. max_tasks is useful for bounded workers/tests."""
        processed = 0
        idle_ticks = 0
        final_label = ""
        while not self._stopped:
            # Idle workers (no claimable task) must also notice a controller
            # kill; claim_once's check only fires when a task is being claimed.
            if self.is_killed():
                self._stopped = True
                _log.info("worker %s killed by controller while idle; stopping", self.agent_id)
                break
            result = await self.run_once()
            if result is None:
                idle_ticks += 1
                if max_tasks is not None and processed >= max_tasks:
                    break
                # A bounded worker should exit after the first idle round when
                # no task was ever claimed; otherwise a finite agent_worker
                # process waits forever for work that will never arrive.
                if max_tasks is not None and processed == 0 and idle_ticks >= 1:
                    break
                await asyncio.sleep(self.poll_interval)
                continue

            idle_ticks = 0
            processed += 1
            if result.final_label:
                # 终止 label 声明：worker 显式结束，不再空转
                _log.info(
                    "worker %s finished with terminal label %s after %d tasks",
                    self.agent_id,
                    result.final_label,
                    processed,
                )
                final_label = result.final_label
                break
            if max_tasks is not None and processed >= max_tasks:
                break

        stats: Dict[str, Any] = {"processed": processed, "idle_ticks": idle_ticks}
        if final_label:
            stats["final_label"] = final_label
        return stats

    def _capture_task_result(self, task: Dict[str, Any], result: Dict[str, Any]) -> Optional[str]:
        focus = _loads_json(task.get("focus_params"), {})
        metadata = {
            "task_type": task.get("task_type"),
            "task_intent": task.get("task_intent"),
            "required_role": task.get("required_role"),
            "focus_params": focus,
            "worker_role": self.role,
            "model_profile": task.get("model_profile") or {},
            **result["metadata"],
        }
        if result["tags"]:
            metadata["tags"] = result["tags"]
        if result["title"]:
            metadata["title"] = result["title"]
        if result["intent"]:
            metadata["intent"] = result["intent"]

        ctx = CaptureContext(
            source=CaptureSource.TASK_RESULT,
            content=result["content"],
            source_agent=self.agent_id,
            source_run_id=self.run_id,
            source_task_id=task["task_id"],
            metadata=metadata,
        )
        return capture(self.db, ctx, auto_classify=True)

    def _ensure_registered(self) -> None:
        if not self._registered:
            self.register()


def build_task_context(db, task: Dict[str, Any], max_entries: int = 5) -> str:
    """Build a compact KB context for a claimed market task.

    Includes the graph's shared signal board when the task is
    graph-affiliated (see :mod:`swarm.signal_board`).
    """
    focus = _loads_json(task.get("focus_params"), {})
    context_ids = focus.get("context_entry_ids", [])
    if not isinstance(context_ids, list):
        context_ids = []
    run_id = task.get("run_id")

    parts = [
        f"## Task\n{task.get('task_type')} for {task.get('required_role') or 'any-role'}",
        f"Intent: {task.get('task_intent') or ''}",
        f"Reason: {focus.get('reason') or ''}",
    ]

    # Shared signal board: graph-scoped blackboard published by probe nodes,
    # read by every downstream worker (shared context for narrow-scope work).
    try:
        from .signal_board import build_signal_context

        board_ctx = build_signal_context(db, task)
        if board_ctx:
            parts.append(board_ctx)
    except Exception:  # noqa: BLE001 — signal board must never break claiming
        pass

    if run_id:
        try:
            run = db.fetch_one(
                "SELECT conversation_summary FROM swarm_runs WHERE run_id = ?",
                (run_id,),
            )
            if run and run["conversation_summary"]:
                parts.append("\n## Run Summary\n" + run["conversation_summary"][:1200])
        except Exception:
            pass

    if context_ids:
        parts.append("\n## Context Entries")
        for entry_id in context_ids[:max_entries]:
            row = db.fetch_one(
                """SELECT id, title, content, knowledge_type, level, domain, tags
                   FROM knowledge_entries WHERE id = ?""",
                (entry_id,),
            )
            if not row:
                continue
            parts.append(
                "\n".join([
                    f"### {sanitize_single_line(row['title'] or row['id'])}",
                    f"type={row['knowledge_type']} level={row['level']} domain={row['domain']}",
                    mark_untrusted((row["content"] or "")[:800]),
                ])
            )

    if run_id:
        try:
            raw_events = db.fetch_all(
                """SELECT source_agent, source, capture_status, filter_reason,
                          content, created_at
                   FROM raw_agent_events
                   WHERE run_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (run_id, max_entries),
            )
        except Exception:
            raw_events = []
        if raw_events:
            event_parts = []
            for event in reversed(raw_events):
                status = event["capture_status"]
                # filtered 事件（low_signal 等）不注入上下文——被过滤的内容
                # 仍是不可信文本，且对任务无信息价值（审计 A4）
                if status == "filtered":
                    continue
                reason = f" reason={event['filter_reason']}" if event["filter_reason"] else ""
                event_parts.append(
                    "\n".join([
                        f"### {event['created_at']} {event['source_agent']} {event['source']} status={status}{reason}",
                        mark_untrusted((event["content"] or "")[:500], source="事件"),
                    ])
                )
            if event_parts:
                parts.append("\n## Recent Raw Handoff Events")
                parts.extend(event_parts)

    # 第 1 层 (2026-08-11, migration 008; 2026-08-12 升级为内容级注入):
    # 角色技能包。load_skills 条目解析为 skills/*.md 的真实技能内容注入
    # (migration 009 将默认条目指向技能文件名), 解析不到的旧条目按原样
    # 透传为指令, 向后兼容。
    # ablation 开关: SWARM_SKILL_PACKS=0 时禁用 (基线组), 默认启用。
    import os as _os

    if _os.environ.get("SWARM_SKILL_PACKS", "1") != "0":
        try:
            from .skills import inject_skills_context

            profile = task.get("model_profile") or {}
            skills = profile.get("load_skills") or []
            if isinstance(skills, str):
                skills = _loads_json(skills, [])
            # 任务级技能 (migration 010 索引表推导, 写入 focus task_skills):
            # 合并并去重, 任务级优先 (索引表是该任务该做的, 角色级是兜底)。
            task_skills = focus.get("task_skills") or []
            if isinstance(task_skills, str):
                task_skills = _loads_json(task_skills, [])
            if isinstance(task_skills, list) and task_skills:
                merged: List[str] = []
                seen = set()
                for s in list(task_skills) + list(skills):
                    key = str(s).strip()
                    if key and key not in seen:
                        seen.add(key)
                        merged.append(key)
                skills = merged
            if isinstance(skills, list) and skills:
                inject_skills_context(parts, skills)
        except Exception:  # noqa: BLE001 — 技能注入失败绝不阻断任务
            pass

    # 任务工具白名单 (migration 010 索引表推导, focus task_tools):
    # 注入上下文让 agent 知道该任务可用哪些工具 (执行强制留后续)。
    try:
        task_tools = focus.get("task_tools") or []
        if isinstance(task_tools, str):
            task_tools = _loads_json(task_tools, [])
        if isinstance(task_tools, list) and task_tools:
            parts.append(
                "## Task Tool Allowlist\n"
                + "\n".join(f"- {t}" for t in task_tools)
                + "\n(索引表指定该任务可用工具; 优先使用, 超出需说明理由)"
            )
    except Exception:  # noqa: BLE001 — 工具注入失败绝不阻断任务
        pass

    # Role catalog (migration 017): role brief + blackboard access are now
    # first-class, editable role data.  Missing/old DB falls back to the
    # existing model_profiles behaviour and the worker still runs.
    try:
        from .role_catalog import get_role_catalog, inject_role_context

        role = task.get("required_role") or task.get("model_profile", {}).get("role") or "custom"
        role_def = get_role_catalog(db, role)
        if role_def:
            inject_role_context(parts, role_def)
    except Exception:  # noqa: BLE001 — role catalog must never block a task
        pass

    # 第 2 层 (2026-08-12, migration 009): 角色 MCP 工具段。
    # 蜂群自持 MCP 客户端 (src/swarm/mcp_client.py + scripts/mcp_tool.py),
    # 不依赖 Hermes 的 config.yaml mcp_servers; 注入仅读静态配置, 不拉起进程。
    try:
        from .mcp_client import registry_tool_prompt

        profile = task.get("model_profile") or {}
        servers = profile.get("mcp_servers") or []
        if isinstance(servers, str):
            servers = _loads_json(servers, [])
        if isinstance(servers, list) and servers:
            mcp_block = registry_tool_prompt(servers)
            if mcp_block:
                parts.append(mcp_block)
    except Exception:  # noqa: BLE001 — MCP 注入失败绝不阻断任务
        pass

    return "\n\n".join(parts)


def run_worker_sync(worker: SwarmWorker, max_tasks: Optional[int] = None) -> Dict[str, int]:
    """Synchronous helper for simple scripts."""
    return asyncio.run(worker.run_loop(max_tasks=max_tasks))
