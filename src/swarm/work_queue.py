"""
Swarm work market — shared task pool for stigmergic coordination.

The market uses agent_tasks as the durable queue. Any agent can publish work
from a finding, and live agents claim tasks by role. This is the concrete
difference from a stage pipeline: findings fan out into multiple independent
work items that can be claimed concurrently.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from .model_config import get_model_profile
from .task_skills import index_to_focus, resolve_task_skills


ROLE_BY_TASK_TYPE = {
    "scan": "scanner",
    "analyze": "analyst",
    "exploit": "exploiter",
    "report": "reporter",
    "research": "researcher",
}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _normalize_context_ids(context_entry_ids: Optional[List[str]]) -> List[str]:
    return sorted(str(eid) for eid in (context_entry_ids or []) if eid)


def build_signal_key(
    task_type: str,
    required_role: str,
    context_entry_ids: Optional[List[str]] = None,
    intent: str = "",
    signal_type: str = "knowledge",
) -> str:
    """Build a stable dedupe key for active market tasks."""
    context_key = ",".join(_normalize_context_ids(context_entry_ids)) or "none"
    return f"{signal_type}:{required_role}:{task_type}:{intent or 'any'}:{context_key}"


def publish_work_task(
    db,
    run_id: str,
    task_type: str,
    required_role: Optional[str],
    reason: str,
    context_entry_ids: Optional[List[str]] = None,
    parent_task_id: Optional[str] = None,
    source_agent: str = "system",
    intent: str = "",
    priority: int = 50,
    metadata: Optional[Dict[str, Any]] = None,
    model_profile_id: Optional[str] = None,
    signal_key: Optional[str] = None,
    generation: Optional[int] = None,
    commit: bool = True,
) -> str:
    """
    Publish a task into the shared market.

    Duplicate active tasks with the same signal_key are collapsed. The returned
    task_id is the new task or the existing active task for that signal.
    """
    if not run_id:
        raise ValueError("run_id is required")

    role = required_role or ROLE_BY_TASK_TYPE.get(task_type, "custom")
    # 任务→角色→技能 索引表 (migration 010): 静态查表, 可编辑。
    # 命中 → 角色/技能/工具随任务固化; 未命中 → 回退旧映射 (行为不变)。
    index = resolve_task_skills(db, task_type)
    if index and index.get("role"):
        role = required_role or index["role"]
    profile = get_model_profile(db, role, profile_id=model_profile_id)
    selected_profile_id = profile["profile_id"] if profile else None
    if generation is None and parent_task_id:
        parent = db.fetch_one("SELECT iteration FROM agent_tasks WHERE task_id = ?", (parent_task_id,))
        generation = int(parent["iteration"] or 1) + 1 if parent else 1
    task_generation = max(1, int(generation or 1))
    context_ids = _normalize_context_ids(context_entry_ids)
    key = signal_key or build_signal_key(
        task_type=task_type,
        required_role=role,
        context_entry_ids=context_ids,
        intent=intent,
    )
    task_id = str(uuid.uuid4())
    focus = {
        "reason": reason,
        "context_entry_ids": context_ids,
        "required_role": role,
        "source_agent": source_agent,
        "signal_key": key,
        "model_profile_id": selected_profile_id,
        "generation": task_generation,
        **index_to_focus(index),
        **(metadata or {}),
    }

    cur = db.execute(
        """INSERT OR IGNORE INTO agent_tasks
           (task_id, run_id, agent_id, parent_task_id, task_type, task_intent,
            focus_params, iteration, status, required_role, priority, signal_key, model_profile_id)
           VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (
            task_id,
            run_id,
            parent_task_id,
            task_type,
            intent or task_type,
            _json_text(focus),
            task_generation,
            role,
            max(0, min(100, int(priority))),
            key,
            selected_profile_id,
        ),
    )
    if cur.rowcount == 0:
        existing = db.fetch_one(
            """SELECT task_id, model_profile_id FROM agent_tasks
               WHERE run_id = ? AND signal_key = ?
                 AND status IN ('pending', 'running')
               ORDER BY created_at ASC LIMIT 1""",
            (run_id, key),
        )
        if existing:
            task_id = existing["task_id"]
            if selected_profile_id and not existing["model_profile_id"]:
                db.execute(
                    "UPDATE agent_tasks SET model_profile_id = ? WHERE task_id = ?",
                    (selected_profile_id, task_id),
                )

    if commit:
        db.conn.commit()
    return task_id


def publish_tasks_for_knowledge(
    db,
    entry_id: str,
    classification: Dict[str, Any],
    run_id: Optional[str],
    source_agent: str = "system",
    parent_task_id: Optional[str] = None,
    commit: bool = True,
) -> List[Dict[str, Any]]:
    """
    Fan out one knowledge entry into independent market tasks.

    The rules intentionally emit multiple roles for high-value findings so the
    swarm can analyze, verify/exploit, and report in parallel.
    """
    if not run_id:
        return []

    ktype = classification.get("knowledge_type", "")
    intent = classification.get("knowledge_intent", "")
    level = int(classification.get("level") or 1)
    tasks: List[Dict[str, Any]] = []

    def add(task_type: str, role: str, reason: str, priority: int):
        tasks.append({
            "task_type": task_type,
            "required_role": role,
            "reason": reason,
            "priority": priority,
        })

    if (ktype, intent) == ("vulnerability", "attack"):
        add("analyze", "analyst", f"独立分析漏洞发现 [{entry_id[:8]}] 的影响、根因和边界条件", 80)
        add("exploit", "exploiter", f"在授权范围内验证漏洞发现 [{entry_id[:8]}] 的可利用性", 90)
        add("report", "reporter", f"将漏洞发现 [{entry_id[:8]}] 纳入滚动报告并持续更新证据", 65)
    elif (ktype, intent) == ("technique", "attack"):
        add("analyze", "analyst", f"分析攻击技术 [{entry_id[:8]}] 的适用条件和检测线索", 70)
        add("exploit", "exploiter", f"评估攻击技术 [{entry_id[:8]}] 是否适用于当前目标", 75)
    elif (ktype, intent) == ("pattern", "enumerate"):
        add("scan", "scanner", f"根据枚举模式 [{entry_id[:8]}] 扩展探索相邻资产和参数", 70)
        add("analyze", "analyst", f"归纳枚举模式 [{entry_id[:8]}] 的覆盖缺口", 55)
    elif (ktype, intent) == ("strategy", "defend"):
        add("report", "reporter", f"把防御/缓解策略 [{entry_id[:8]}] 合入当前报告", 60)
    elif level >= 3:
        add("report", "reporter", f"把高置信知识 [{entry_id[:8]}] 合入当前报告", 55)

    published: List[Dict[str, Any]] = []
    for task in tasks:
        task_id = publish_work_task(
            db,
            run_id=run_id,
            task_type=task["task_type"],
            required_role=task["required_role"],
            reason=task["reason"],
            context_entry_ids=[entry_id],
            parent_task_id=parent_task_id,
            source_agent=source_agent,
            intent=intent,
            priority=task["priority"],
            metadata={
                "knowledge_type": ktype,
                "knowledge_intent": intent,
                "market_source": "capture",
            },
            commit=False,
        )
        published.append({**task, "task_id": task_id})

    if commit and published:
        db.conn.commit()
    return published


def poll_work_tasks(
    db,
    run_id: Optional[str] = None,
    required_role: Optional[str] = None,
    status: str = "pending",
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Read visible work items without claiming them."""
    conditions = ["status = ?"]
    params: List[Any] = [status]
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)
    if required_role:
        conditions.append("(required_role = ? OR required_role IS NULL)")
        params.append(required_role)

    sql = f"""SELECT * FROM agent_tasks
              WHERE {" AND ".join(conditions)}
              ORDER BY priority DESC, created_at ASC
              LIMIT ?"""
    params.append(limit)
    return [dict(r) for r in db.fetch_all(sql, tuple(params))]


def claim_work_tasks(
    db,
    run_id: str,
    agent_id: str,
    role: str,
    limit: int = 1,
) -> List[Dict[str, Any]]:
    """
    Atomically claim pending tasks matching an agent role.

    Multiple agents may race on the same visible candidate list; only one UPDATE
    can transition a task from pending to running.
    """
    candidates = poll_work_tasks(
        db, run_id=run_id, required_role=role, status="pending", limit=limit
    )
    claimed: List[Dict[str, Any]] = []

    for task in candidates:
        cur = db.execute(
            """UPDATE agent_tasks
               SET status = 'running',
                   agent_id = ?,
                   claimed_at = datetime('now'),
                   started_at = COALESCE(started_at, datetime('now')),
                   updated_at = datetime('now'),
                   claim_count = COALESCE(claim_count, 0) + 1
               WHERE task_id = ?
                 AND status = 'pending'
                 AND (agent_id IS NULL OR agent_id = ?)
                 AND (required_role = ? OR required_role IS NULL)""",
            (agent_id, task["task_id"], agent_id, role),
        )
        if cur.rowcount == 1:
            task["status"] = "running"
            task["agent_id"] = agent_id
            claimed.append(task)

    if claimed:
        db.conn.commit()
    return claimed


def recover_stale_work_claims(db, stale_seconds: int = 900) -> int:
    """Release work tasks stuck in running without completion."""
    stale = max(1, int(stale_seconds or 900))
    cur = db.execute(
        """UPDATE agent_tasks
           SET status = 'pending',
               agent_id = NULL,
               claimed_at = NULL,
               updated_at = datetime('now')
           WHERE status = 'running'
             AND claimed_at IS NOT NULL
             AND (julianday('now') - julianday(claimed_at)) * 86400 > ?""",
        (stale,),
    )
    db.conn.commit()
    return cur.rowcount


def complete_work_task(
    db,
    task_id: str,
    result_summary: Optional[Dict[str, Any]] = None,
    token_cost: int = 0,
) -> None:
    """Mark a claimed market task complete."""
    db.execute(
        """UPDATE agent_tasks
           SET status = 'completed',
               result_summary = ?,
               token_cost = COALESCE(token_cost, 0) + ?,
               ended_at = datetime('now'),
               updated_at = datetime('now')
           WHERE task_id = ? AND status = 'running'""",
        (_json_text(result_summary or {}), max(0, int(token_cost or 0)), task_id),
    )
    db.conn.commit()


def fail_work_task(db, task_id: str, reason: str = "") -> None:
    """Mark a claimed market task failed."""
    db.execute(
        """UPDATE agent_tasks
           SET status = 'failed',
               result_summary = ?,
               ended_at = datetime('now'),
               updated_at = datetime('now')
           WHERE task_id = ? AND status = 'running'""",
        (_json_text({"error": reason}), task_id),
    )
    db.conn.commit()
