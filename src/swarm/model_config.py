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
    keys = row.keys() if hasattr(row, "keys") else []
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
        # 第 1 层 (migration 008): 角色技能包。旧库缺列时防御性回退。
        "load_skills": _loads(row["load_skills"], []) if keys and "load_skills" in keys and row["load_skills"] else [],
        "tool_allowlist": _loads(row["tool_allowlist"], []) if keys and "tool_allowlist" in keys and row["tool_allowlist"] else [],
        # 第 2 层 (migration 009): 角色可用的 MCP 服务器 (mcp_servers.json 键)。
        "mcp_servers": _loads(row["mcp_servers"], []) if keys and "mcp_servers" in keys and row["mcp_servers"] else [],
        # 第 3 层 (migration 020): 免费池分层。旧库缺列回退 'paid'。
        "tier": row["tier"] if keys and "tier" in keys else "paid",
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
    load_skills: Optional[List[str]] = None,
    tool_allowlist: Optional[List[str]] = None,
    mcp_servers: Optional[List[str]] = None,
    commit: bool = True,
) -> str:
    """Create or update a model profile owned by the swarm."""
    pid = profile_id or f"{role}-{provider}-{model}".replace("/", "-").replace(":", "-")

    if is_default:
        db.execute("UPDATE model_profiles SET is_default = 0 WHERE role = ?", (role,))

    db.execute(
        """INSERT INTO model_profiles
           (profile_id, role, provider, model, priority, is_default, enabled,
            max_tokens, temperature, tool_policy, system_prompt, metadata,
            load_skills, tool_allowlist, mcp_servers, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
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
               load_skills = excluded.load_skills,
               tool_allowlist = excluded.tool_allowlist,
               mcp_servers = excluded.mcp_servers,
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
            _json_text(load_skills or []),
            _json_text(tool_allowlist or []),
            _json_text(mcp_servers or []),
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


# ── 免费池分层 (migration 020, 2026-08-22) ────────────────────────────────
# 模型对照表: 角色默认 profile 的 tier 决定走免费池还是付费通道。
# - tier='free' → 免费池轮询 (OpenCode Zen / ZenMux free), 按日限额 + 调用数
#   均衡选择; 全部超限/不可用 → 自动降级到该角色的付费 profile。
# - tier='paid' → 保持现状 (executor 用自己的付费通道)。
# 免费池候选 = model_profiles 中 tier='free' AND enabled=1 的所有行
# (跨角色共享, 按 priority 与当日用量轮询; 种子见 migration 020)。

FREE_POOL_ENV_TOKEN_LIMIT = "SWARM_FREE_DAILY_TOKENS"   # 每模型每日 token 限额 (默认 1M)
FREE_POOL_ENV_CALL_LIMIT = "SWARM_FREE_DAILY_CALLS"     # 每模型每日调用限额 (默认 300)
FREE_POOL_DEFAULT_TOKENS = 1_000_000
FREE_POOL_DEFAULT_CALLS = 300


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def record_model_usage(
    db,
    model_key: str,
    tokens: int = 0,
    calls: int = 1,
    commit: bool = True,
) -> None:
    """记录免费池模型当日用量 (model_usage_daily UPSERT)。"""
    if not model_key:
        return
    db.execute(
        """INSERT INTO model_usage_daily (model_key, usage_date, tokens, calls, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'))
           ON CONFLICT(model_key, usage_date) DO UPDATE SET
               tokens = tokens + excluded.tokens,
               calls = calls + excluded.calls,
               updated_at = datetime('now')""",
        (model_key, _today(), max(0, int(tokens or 0)), max(0, int(calls or 1))),
    )
    if commit:
        db.conn.commit()


def _daily_usage(db, model_key: str) -> Dict[str, int]:
    row = db.fetch_one(
        "SELECT tokens, calls FROM model_usage_daily WHERE model_key = ? AND usage_date = ?",
        (model_key, _today()),
    )
    return {"tokens": int(row["tokens"] or 0) if row else 0,
            "calls": int(row["calls"] or 0) if row else 0}


def _free_limits(profile: Dict[str, Any]) -> Dict[str, int]:
    import os

    meta = profile.get("metadata") or {}
    # env 全局覆盖优先于 profile 级 metadata (可临时调整个池子限额)
    token_limit = int(os.environ.get(
        FREE_POOL_ENV_TOKEN_LIMIT) or meta.get("daily_limit_tokens") or FREE_POOL_DEFAULT_TOKENS)
    call_limit = int(os.environ.get(
        FREE_POOL_ENV_CALL_LIMIT) or meta.get("daily_limit_calls") or FREE_POOL_DEFAULT_CALLS)
    return {"tokens": token_limit, "calls": call_limit}


def _pick_free_model(db, role: str) -> Optional[Dict[str, Any]]:
    """从免费池选出本次执行使用的模型。

    候选: 所有 tier='free' AND enabled=1 的 profile。
    过滤: 当日用量未超限 (tokens < 限额 且 calls < 调用限额)。
    排序: priority DESC → 当日 calls ASC (均衡轮询) → provider/model。
    返回 profile + 路由信息; 无可选时返回 None (调用方降级付费)。
    """
    rows = db.fetch_all(
        """SELECT * FROM model_profiles
           WHERE tier = 'free' AND enabled = 1
           ORDER BY priority DESC, provider, model"""
    )
    if not rows:
        return None
    candidates = []
    for row in rows:
        if row is None:
            continue
        profile = _row_to_profile(row)
        if profile is None:
            continue
        usage = _daily_usage(db, profile["model"])
        limits = _free_limits(profile)
        if usage["tokens"] >= limits["tokens"] or usage["calls"] >= limits["calls"]:
            continue
        candidates.append((profile, usage["calls"]))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: (-pair[0]["priority"], pair[1], pair[0]["model"]))
    return candidates[0][0]


def resolve_execution_model(db, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析任务的执行模型 (模型对照表入口)。

    语义 (免费优先, 白名单角色):
      1. 取角色默认 profile (现有语义, 多数角色为 paid)。
      2. 仅当该角色配置了 tier='free' 的启用 profile (白名单, 种子见
         migration 020) 时, 才尝试免费池: 命中 → engine='opencode' +
         具体免费模型; 全超限 → 降级该角色默认付费 profile。
      3. 非白名单角色 (analyst/exploiter 等) 永远走付费通道,
         engine='hermes' (executor 用自己的付费模型, 现状)。

    返回 profile 字典, 附加路由字段:
      - tier:          'free' | 'paid'
      - engine:        'opencode' (免费池, executor 用 opencode run 执行)
                       | 'hermes' (付费, executor 用 hermes chat 执行, 现状)
      - resolved_model: 免费池具体模型 id (engine='opencode' 时有效),
                       如 'zenmux/z-ai/glm-5.3-free' / 'opencode/nemotron-3-ultra-free'
      - pool_used:     本次是否命中免费池
    """
    role = task.get("required_role") or "custom"
    default_profile = get_model_profile(db, role, profile_id=task.get("model_profile_id"))
    if not default_profile:
        return None

    # 白名单检查: 该角色配置了 free profile 才进免费池
    free_row = db.fetch_one(
        """SELECT 1 FROM model_profiles
           WHERE role = ? AND tier = 'free' AND enabled = 1
           LIMIT 1""",
        (role,),
    )
    if free_row:
        free_model = _pick_free_model(db, role)
        if free_model is not None:
            resolved = dict(free_model)
            resolved["tier"] = "free"
            resolved["engine"] = "opencode"
            # opencode --model 需要 provider/model 完整 id
            # (model 已含 '/' 时视为已完整, 直接使用)
            fm = free_model["model"] or ""
            resolved["resolved_model"] = fm if "/" in fm else f"{free_model['provider']}/{fm}"
            resolved["pool_used"] = True
            resolved["role"] = role
            return resolved
        # 免费池全超限/不可用 → 降级该角色默认付费 profile
        resolved = dict(default_profile)
        resolved["tier"] = "paid"
        resolved["engine"] = "hermes"
        resolved["resolved_model"] = None
        resolved["pool_used"] = False
        resolved["role"] = role
        return resolved

    # 非白名单角色 → 付费通道 (现状语义)
    resolved = dict(default_profile)
    resolved["tier"] = "paid"
    resolved["engine"] = "hermes"
    resolved["resolved_model"] = None
    resolved["pool_used"] = False
    resolved["role"] = role
    return resolved


def free_pool_status(db) -> List[Dict[str, Any]]:
    """免费池诊断快照: 每个 free 模型 + 当日用量/限额。"""
    rows = db.fetch_all(
        """SELECT * FROM model_profiles
           WHERE tier = 'free' AND enabled = 1
           ORDER BY priority DESC, provider, model"""
    )
    result = []
    for row in rows:
        if row is None:
            continue
        profile = _row_to_profile(row)
        if profile is None:
            continue
        usage = _daily_usage(db, profile["model"])
        limits = _free_limits(profile)
        result.append({
            "profile_id": profile["profile_id"],
            "role": profile["role"],
            "model": profile["model"],
            "tokens_today": usage["tokens"],
            "calls_today": usage["calls"],
            "limit_tokens": limits["tokens"],
            "limit_calls": limits["calls"],
            "available": usage["tokens"] < limits["tokens"] and usage["calls"] < limits["calls"],
        })
    return result


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
