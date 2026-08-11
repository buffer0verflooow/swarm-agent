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
    record_swarm_event,
    resolve_task_model_profile,
)
from .artifacts import verify_artifacts
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

    def claim_once(self) -> Optional[Dict[str, Any]]:
        """Claim one task and return it with built context, without executing."""
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
        model_profile = resolve_task_model_profile(self.db, task)
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

            if not normalized["success"]:
                error = normalized["error"] or "executor reported failure"
                fail_work_task(self.db, task_id, error)
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
                fail_work_task(self.db, task_id, error)
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
                    fail_work_task(self.db, task_id, error)
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
            fail_work_task(self.db, task_id, str(exc))
            self.lifecycle.beat(current_task_id=None, load=0.0)
            _log.exception("worker task failed: task_id=%s", task_id)
            return WorkerResult(task_id=task_id, status="failed", error=str(exc))

    async def run_loop(self, max_tasks: Optional[int] = None) -> Dict[str, int]:
        """Run until stopped. max_tasks is useful for bounded workers/tests."""
        processed = 0
        idle_ticks = 0
        final_label = ""
        while not self._stopped:
            result = await self.run_once()
            if result is None:
                idle_ticks += 1
                if max_tasks is not None and processed >= max_tasks:
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
                    f"### {row['title'] or row['id']}",
                    f"type={row['knowledge_type']} level={row['level']} domain={row['domain']}",
                    (row["content"] or "")[:800],
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
            parts.append("\n## Recent Raw Handoff Events")
            for event in reversed(raw_events):
                status = event["capture_status"]
                reason = f" reason={event['filter_reason']}" if event["filter_reason"] else ""
                parts.append(
                    "\n".join([
                        f"### {event['created_at']} {event['source_agent']} {event['source']} status={status}{reason}",
                        (event["content"] or "")[:500],
                    ])
                )

    return "\n\n".join(parts)


def run_worker_sync(worker: SwarmWorker, max_tasks: Optional[int] = None) -> Dict[str, int]:
    """Synchronous helper for simple scripts."""
    return asyncio.run(worker.run_loop(max_tasks=max_tasks))
