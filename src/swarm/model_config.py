"""
Swarm-owned model configuration and run summaries.

The swarm runtime uses model profiles while executing internal role tasks.
External clients submit top-level tasks and retrieve run results; they should
not decide or claim internal role/model assignments directly.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Iterator as IteratorT, List, Optional


def _json_text(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


# 调用级模型作用域（DeepTutor request-scoped 模型选择移植）：
# with model_scope(...) 块内 get_model_profile()/resolve_task_model_profile()
# 返回覆盖配置；块外自动恢复。contextvars 实现，async 安全。
_model_scope: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "model_scope", default=None
)


@contextlib.contextmanager
def model_scope(
    db,
    role: str,
    profile_id: Optional[str] = None,
    **overrides,
) -> IteratorT[Optional[Dict[str, Any]]]:
    """调用级模型作用域。

    with 块内 get_model_profile()/resolve_task_model_profile() 解析 role 的
    profile 并应用 overrides（可覆盖 provider/model/max_tokens/temperature/
    priority 等字段）；块外自动恢复。overrides 为空时仅返回解析结果不改变行为。

    用法:
        with model_scope(db, "capture", model="deepseek-v4-flash"):
            prof = get_model_profile(db, "capture")  # model 被覆盖
        prof = get_model_profile(db, "capture")      # 恢复原值

    仅对声明时的 role 生效；其他 role 的解析不受影响。
    """
    base = get_model_profile(db, role, profile_id=profile_id)
    if base is not None:
        scoped = dict(base)
        scoped["role"] = role  # 以声明 role 为准（base 可能来自 custom 兜底）
        scoped.update({k: v for k, v in overrides.items() if v is not None})
    else:
        # role 无 profile 时构造轻量覆盖配置
        scoped = {"role": role, **{k: v for k, v in overrides.items() if v is not None}}
    token = _model_scope.set(scoped)
    try:
        yield scoped
    finally:
        _model_scope.reset(token)


def _row_to_profile(row) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "profile_id": row["profile_id"],
        "role": row["role"],
        "provider": row["provider"],
        "model": row["model"],
        "priority": row["priority"],
        "is_default": bool(row["is_default"]),
        "enabled": bool(row["enabled"]),
        "max_tokens": row["max_tokens"],
        "temperature": row["temperature"],
        "tool_policy": _loads(row["tool_policy"], {}),
        "system_prompt": row["system_prompt"] or "",
        "metadata": _loads(row["metadata"], {}),
    }


def list_model_profiles(db, role: Optional[str] = None, enabled_only: bool = False) -> List[Dict[str, Any]]:
    conditions = []
    params: List[Any] = []
    if role:
        conditions.append("role = ?")
        params.append(role)
    if enabled_only:
        conditions.append("enabled = 1")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = db.fetch_all(
        f"""SELECT * FROM model_profiles
            {where}
            ORDER BY role, is_default DESC, priority DESC, provider, model""",
        tuple(params),
    )
    return [_row_to_profile(r) for r in rows]


def get_model_profile(
    db,
    role: str,
    profile_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the swarm-selected model profile for a role.

    调用级作用域（model_scope）优先：scope 内且 role 匹配时直接返回覆盖配置，
    不再查库。
    """
    scoped = _model_scope.get()
    if scoped is not None and scoped.get("role") == role:
        return scoped
    if profile_id:
        row = db.fetch_one(
            "SELECT * FROM model_profiles WHERE profile_id = ? AND enabled = 1",
            (profile_id,),
        )
        if row:
            return _row_to_profile(row)

    row = db.fetch_one(
        """SELECT * FROM model_profiles
           WHERE role = ? AND enabled = 1
           ORDER BY is_default DESC, priority DESC, created_at ASC
           LIMIT 1""",
        (role,),
    )
    if row:
        return _row_to_profile(row)

    row = db.fetch_one(
        """SELECT * FROM model_profiles
           WHERE role = 'custom' AND enabled = 1
           ORDER BY is_default DESC, priority DESC, created_at ASC
           LIMIT 1"""
    )
    return _row_to_profile(row)


def upsert_model_profile(
    db,
    role: str,
    provider: str,
    model: str,
    profile_id: Optional[str] = None,
    priority: int = 50,
    is_default: bool = False,
    enabled: bool = True,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    tool_policy: Optional[Dict[str, Any]] = None,
    system_prompt: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> str:
    """Create or update a model profile owned by the swarm."""
    pid = profile_id or f"{role}-{provider}-{model}".replace("/", "-").replace(":", "-")

    if is_default:
        db.execute("UPDATE model_profiles SET is_default = 0 WHERE role = ?", (role,))

    db.execute(
        """INSERT INTO model_profiles
           (profile_id, role, provider, model, priority, is_default, enabled,
            max_tokens, temperature, tool_policy, system_prompt, metadata, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(profile_id) DO UPDATE SET
               role = excluded.role,
               provider = excluded.provider,
               model = excluded.model,
               priority = excluded.priority,
               is_default = excluded.is_default,
               enabled = excluded.enabled,
               max_tokens = excluded.max_tokens,
               temperature = excluded.temperature,
               tool_policy = excluded.tool_policy,
               system_prompt = excluded.system_prompt,
               metadata = excluded.metadata,
               updated_at = datetime('now')""",
        (
            pid,
            role,
            provider,
            model,
            max(0, min(100, int(priority))),
            1 if is_default else 0,
            1 if enabled else 0,
            max_tokens,
            temperature,
            _json_text(tool_policy),
            system_prompt,
            _json_text(metadata),
        ),
    )
    if commit:
        db.conn.commit()
    return pid


def assign_task_model_profile(db, task_id: str, role: str, profile_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Persist the selected model profile on a task."""
    profile = get_model_profile(db, role, profile_id=profile_id)
    if not profile:
        return None
    db.execute(
        "UPDATE agent_tasks SET model_profile_id = ? WHERE task_id = ?",
        (profile["profile_id"], task_id),
    )
    return profile


def resolve_task_model_profile(db, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    role = task.get("required_role") or "custom"
    profile_id = task.get("model_profile_id")
    return get_model_profile(db, role, profile_id=profile_id)


def record_swarm_event(
    db,
    run_id: str,
    event_type: str,
    source: str,
    content: str,
    agent_id: Optional[str] = None,
    task_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> str:
    event_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO swarm_conversation_events
           (event_id, run_id, event_type, source, agent_id, task_id, content, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            run_id,
            event_type,
            source,
            agent_id,
            task_id,
            content,
            _json_text(metadata),
        ),
    )
    if commit:
        db.conn.commit()
    return event_id


def build_run_summary(db, run_id: str, limit_events: int = 10) -> Dict[str, Any]:
    """Build a compact conversation/run summary for external clients."""
    run = db.fetch_one("SELECT * FROM swarm_runs WHERE run_id = ?", (run_id,))
    if not run:
        raise ValueError(f"run not found: {run_id}")

    task_rows = db.fetch_all(
        """SELECT status, required_role, task_type, COUNT(*) AS c
           FROM agent_tasks WHERE run_id = ?
           GROUP BY status, required_role, task_type
           ORDER BY status, required_role, task_type""",
        (run_id,),
    )
    knowledge_rows = db.fetch_all(
        """SELECT knowledge_type, level, COUNT(*) AS c
           FROM knowledge_entries WHERE source_run_id = ?
           GROUP BY knowledge_type, level
           ORDER BY level DESC, knowledge_type""",
        (run_id,),
    )
    events = db.fetch_all(
        """SELECT event_type, source, agent_id, task_id, content, created_at
           FROM swarm_conversation_events
           WHERE run_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (run_id, limit_events),
    )
    model_rows = db.fetch_all(
        """SELECT at.required_role, at.model_profile_id, mp.provider, mp.model, COUNT(*) AS c
           FROM agent_tasks at
           LEFT JOIN model_profiles mp ON at.model_profile_id = mp.profile_id
           WHERE at.run_id = ?
           GROUP BY at.required_role, at.model_profile_id, mp.provider, mp.model
           ORDER BY at.required_role, c DESC""",
        (run_id,),
    )

    task_summary = [
        {
            "status": r["status"],
            "role": r["required_role"],
            "task_type": r["task_type"],
            "count": r["c"],
        }
        for r in task_rows
    ]
    knowledge_summary = [
        {
            "knowledge_type": r["knowledge_type"],
            "level": r["level"],
            "count": r["c"],
        }
        for r in knowledge_rows
    ]
    event_summary = [dict(e) for e in reversed(events)]
    model_summary = [
        {
            "role": r["required_role"],
            "profile_id": r["model_profile_id"],
            "provider": r["provider"],
            "model": r["model"],
            "count": r["c"],
        }
        for r in model_rows
    ]

    lines = [
        f"Run {run_id[:8]}: {run['swarm_name']} target={run['target_type']}:{run['target_id']} intent={run['intent']} status={run['status']}",
        "Tasks: " + (", ".join(f"{t['status']}/{t['role'] or 'any'}/{t['task_type']}={t['count']}" for t in task_summary) or "none"),
        "Knowledge: " + (", ".join(f"L{k['level']} {k['knowledge_type']}={k['count']}" for k in knowledge_summary) or "none"),
        "Models: " + (", ".join(
            f"{m['role'] or 'any'}={m['provider'] or 'unknown'}/{m['model'] or m['profile_id'] or 'unassigned'}({m['count']})"
            for m in model_summary
        ) or "none"),
    ]
    if event_summary:
        lines.append("Recent events:")
        lines.extend(f"- [{e['event_type']}] {e['content'][:200]}" for e in event_summary)

    summary_text = "\n".join(lines)
    db.execute(
        "UPDATE swarm_runs SET conversation_summary = ?, summary_updated_at = datetime('now') WHERE run_id = ?",
        (summary_text, run_id),
    )
    db.conn.commit()

    return {
        "run_id": run_id,
        "summary": summary_text,
        "tasks": task_summary,
        "knowledge": knowledge_summary,
        "models": model_summary,
        "events": event_summary,
    }
