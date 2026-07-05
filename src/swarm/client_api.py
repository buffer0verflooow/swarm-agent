"""
Client-facing swarm task API.

Hermes, Claude, Codex, or any other client should use this layer to submit a
top-level task to the swarm and retrieve the run result. Worker claim/complete
APIs remain internal swarm runtime mechanics.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

from .model_config import build_run_summary, record_swarm_event
from .run_manager import create_seeded_swarm_run


TERMINAL_TASK_STATUSES = {"completed", "failed", "timeout"}
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


def _loads_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _target_from_task(task: str) -> str:
    text = " ".join((task or "").split())
    if not text:
        return f"client-task-{uuid.uuid4().hex[:12]}"
    return text[:120]


def submit_swarm_task(
    db,
    task: str,
    client_source: str,
    intent: str = "custom",
    target_type: str = "unknown",
    target_id: str = "",
    profile: str = "balanced",
    swarm_name: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Submit a top-level client request to the swarm.

    The client receives a run_id and later asks for status/result. It does not
    claim role tasks directly.
    """
    objective = (task or "").strip()
    if not objective:
        raise ValueError("task is required")

    source = (client_source or "client").strip() or "client"
    target = target_id or _target_from_task(objective)
    request_id = str(uuid.uuid4())
    result = create_seeded_swarm_run(
        db,
        swarm_name=swarm_name or f"{source}-task",
        intent=intent,
        target_type=target_type,
        target_id=target,
        profile=profile,
        objective=objective,
        config={
            "client_request_id": request_id,
            "client_source": source,
            "client_task": objective,
            "client_metadata": metadata or {},
        },
    )
    run_id = result["run_id"]
    event_id = record_swarm_event(
        db,
        run_id=run_id,
        event_type="client_task_submitted",
        source=source,
        content=objective,
        metadata={
            "request_id": request_id,
            "intent": intent,
            "target_type": target_type,
            "target_id": target,
            "profile": profile,
            **(metadata or {}),
        },
    )

    return {
        "request_id": request_id,
        "run_id": run_id,
        "status": "running",
        "event_id": event_id,
        "seeded_tasks": result["seeded_tasks"],
        "min_agents_by_role": result.get("min_agents_by_role", {}),
        "max_agents": result.get("max_agents"),
    }


def refresh_run_status(db, run_id: str, commit: bool = True) -> str:
    """Update swarm_runs.status from task-market state and return it."""
    run = db.fetch_one("SELECT status FROM swarm_runs WHERE run_id = ?", (run_id,))
    if not run:
        raise ValueError(f"run not found: {run_id}")

    current = run["status"]
    if current in TERMINAL_RUN_STATUSES:
        return current

    rows = db.fetch_all(
        "SELECT status, COUNT(*) AS c FROM agent_tasks WHERE run_id = ? GROUP BY status",
        (run_id,),
    )
    counts = {r["status"]: int(r["c"]) for r in rows}
    if not counts:
        return current

    active = counts.get("pending", 0) + counts.get("running", 0)
    if active > 0:
        if current != "running":
            db.execute(
                "UPDATE swarm_runs SET status = 'running', updated_at = datetime('now') WHERE run_id = ?",
                (run_id,),
            )
            if commit:
                db.conn.commit()
        return "running"

    completed = counts.get("completed", 0)
    new_status = "completed" if completed else "failed"
    db.execute(
        """UPDATE swarm_runs
           SET status = ?,
               ended_at = COALESCE(ended_at, datetime('now')),
               updated_at = datetime('now')
           WHERE run_id = ?""",
        (new_status, run_id),
    )
    if commit:
        db.conn.commit()
    return new_status


def get_swarm_status(db, run_id: str) -> Dict[str, Any]:
    """Return client-facing run status."""
    status = refresh_run_status(db, run_id)
    run = db.fetch_one(
        """SELECT run_id, swarm_name, intent, target_type, target_id, status,
                  config, started_at, ended_at
           FROM swarm_runs WHERE run_id = ?""",
        (run_id,),
    )
    if not run:
        raise ValueError(f"run not found: {run_id}")

    task_rows = db.fetch_all(
        "SELECT status, COUNT(*) AS c FROM agent_tasks WHERE run_id = ? GROUP BY status",
        (run_id,),
    )
    tasks = {r["status"]: int(r["c"]) for r in task_rows}
    return {
        "run_id": run_id,
        "status": status,
        "ready": status in TERMINAL_RUN_STATUSES,
        "swarm_name": run["swarm_name"],
        "intent": run["intent"],
        "target_type": run["target_type"],
        "target_id": run["target_id"],
        "config": _loads_json(run["config"], {}),
        "started_at": run["started_at"],
        "ended_at": run["ended_at"],
        "tasks": tasks,
    }


def _task_results(db, run_id: str) -> List[Dict[str, Any]]:
    rows = db.fetch_all(
        """SELECT task_id, task_type, required_role, status, result_summary, ended_at
           FROM agent_tasks
           WHERE run_id = ? AND status IN ('completed', 'failed', 'timeout')
           ORDER BY
             CASE task_type WHEN 'report' THEN 0 WHEN 'analyze' THEN 1 WHEN 'exploit' THEN 2 ELSE 3 END,
             ended_at DESC""",
        (run_id,),
    )
    return [
        {
            "task_id": r["task_id"],
            "task_type": r["task_type"],
            "role": r["required_role"],
            "status": r["status"],
            "result_summary": _loads_json(r["result_summary"], {}),
            "ended_at": r["ended_at"],
        }
        for r in rows
    ]


def _knowledge_results(db, run_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    rows = db.fetch_all(
        """SELECT id, title, content, knowledge_type, level, source_agent, created_at
           FROM knowledge_entries
           WHERE source_run_id = ? AND status = 'active'
           ORDER BY level DESC, created_at DESC
           LIMIT ?""",
        (run_id, limit),
    )
    return [dict(r) for r in rows]


def _select_result_text(task_results: List[Dict[str, Any]], knowledge: List[Dict[str, Any]], summary_text: str) -> str:
    for task in task_results:
        if task["status"] != "completed":
            continue
        result = task["result_summary"]
        for key in ("content", "summary", "result", "output"):
            value = result.get(key)
            if value:
                return str(value)

    if knowledge:
        lines = []
        for item in knowledge:
            title = item.get("title") or item["id"]
            lines.append(f"[{item['knowledge_type']} L{item['level']}] {title}\n{item['content']}")
        return "\n\n".join(lines)

    return summary_text


def get_swarm_result(db, run_id: str, limit_events: int = 10) -> Dict[str, Any]:
    """Return the client-facing result payload for a swarm run."""
    status_payload = get_swarm_status(db, run_id)
    summary = build_run_summary(db, run_id, limit_events=limit_events)
    task_results = _task_results(db, run_id)
    knowledge = _knowledge_results(db, run_id)
    result_text = _select_result_text(task_results, knowledge, summary["summary"])

    return {
        **status_payload,
        "summary": summary["summary"],
        "result": result_text,
        "details": summary,
        "task_results": task_results,
        "knowledge_results": knowledge,
    }


def wait_for_swarm_result(
    db,
    run_id: str,
    timeout_seconds: float = 300.0,
    poll_interval: float = 5.0,
    limit_events: int = 10,
) -> Dict[str, Any]:
    """Poll until the run is terminal or timeout expires."""
    timeout = max(0.0, float(timeout_seconds))
    interval = max(0.1, float(poll_interval))
    deadline = time.time() + timeout

    while True:
        result = get_swarm_result(db, run_id, limit_events=limit_events)
        if result["ready"] or time.time() >= deadline:
            result["timed_out"] = not result["ready"]
            return result
        time.sleep(interval)
