"""
Spawn 请求机制：子 Agent 写信号 → Orchestrator 异步实例化

这是 stigmergy 模式的核心——Agent 不直接生成子 Agent，而是
在 KB 中留下"spawn 信号"，Orchestrator 轮询检测并统一执行。

单层嵌套限制的解决方案：
  虽然框架禁止子 Agent 直接 spawn 孙 Agent，但通过 KB 中转，
  逻辑上的"孙代"关系得以保留（通过 parent_task_id + context_entry_ids）。

用法:
    from src.swarm.spawner import request_spawn, claim_spawn_requests, mark_spawn_fulfilled

    # Agent 端：发现需要协作
    request_spawn(db, run_id, "scanner-001", "exploiter",
                  reason="发现 SQL injection 漏洞", 
                  context_entry_ids=[entry_id])

    # Orchestrator 端：原子领取后处理
    claimed = claim_spawn_requests(db, run_id)
    for req in claimed:
        agent = spawn_agent(...)
        mark_spawn_fulfilled(db, req["request_id"], agent.id)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

_log = logging.getLogger("swarm_knowledge.spawner")


def _normalized_context_entry_ids(context_entry_ids: List[str] = None) -> List[str]:
    if not context_entry_ids:
        return []
    return sorted({str(entry_id) for entry_id in context_entry_ids if entry_id})


def _decode_context_entry_ids(context_entry_ids: Any) -> List[str]:
    if isinstance(context_entry_ids, str):
        try:
            context_entry_ids = json.loads(context_entry_ids)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(context_entry_ids, list):
        return _normalized_context_entry_ids(context_entry_ids)
    return []


def build_spawn_dedup_key(
    run_id: str,
    requested_role: str,
    reason: str,
    context_entry_ids: List[str] = None,
) -> str:
    """Stable duplicate key for semantically identical pending spawn requests."""
    payload = {
        "run_id": run_id,
        "requested_role": requested_role,
        "reason": (reason or "").strip(),
        "context_entry_ids": _normalized_context_entry_ids(context_entry_ids),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_spawn(
    db,
    run_id: str,
    requesting_agent: str,
    requested_role: str,
    reason: str,
    context_entry_ids: List[str] = None,
    parent_task_id: str = None,
    priority: int = 60,
    ttl_minutes: int = 10,
    chain_depth: int = 0,
    max_chain_depth: int = None,
    commit: bool = True,
) -> str:
    """
    Agent 发现需要协作时调用。不直接生成 Agent，写入信号表。

    使用场景：
        scanner 发现开放了 443 → request_spawn("web_analyst")
        analyst 发现 CVE-2024 → request_spawn("exploiter")
        
    Args:
        db: SwarmDB 实例
        run_id: 当前 swarm run
        requesting_agent: 发起请求的 agent_id
        requested_role: 需要的角色 (scanner/analyst/exploiter/reporter/orchestrator/custom)
        reason: 为什么需要这个 Agent
        context_entry_ids: 触发 spawn 的知识条目 ID 列表（供新 Agent 读取上下文）
        parent_task_id: 父任务 ID（建立逻辑血缘）
        priority: 优先级 0-100
        ttl_minutes: 有效时间（分钟），超时未处理则标记 expired
        chain_depth: 当前链深度 (0=首次发现, 1=第一级追链, ...)
        max_chain_depth: 最大允许链深度 (None=使用系统默认)
        commit: 是否立即提交。嵌入 capture() 事务时传 False。
    Returns:
        request_id
    """
    req_id = str(uuid.uuid4())
    
    # 如果未指定 max_chain_depth, 使用系统默认
    if max_chain_depth is None:
        max_chain_depth = 3  # MAX_CHAIN_DEPTH_DEFAULT

    normalized_context_ids = _normalized_context_entry_ids(context_entry_ids)
    dedup_key = build_spawn_dedup_key(run_id, requested_role, reason, normalized_context_ids)
    existing = db.fetch_one(
        """SELECT request_id FROM spawn_requests
           WHERE run_id = ? AND dedup_key = ? AND status = 'pending'
           LIMIT 1""",
        (run_id, dedup_key),
    )
    if existing:
        return existing["request_id"]

    ttl = max(1, int(ttl_minutes or 10))
    expires_modifier = f"+{ttl} minutes"

    try:
        db.execute(
            """INSERT INTO spawn_requests
               (request_id, run_id, requesting_agent, parent_task_id,
                requested_role, reason, context_entry_ids, dedup_key, priority,
                chain_depth, max_chain_depth, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))""",
            (
                req_id, run_id, requesting_agent, parent_task_id,
                requested_role, reason,
                json.dumps(normalized_context_ids),
                dedup_key,
                max(0, min(100, int(priority))),
                max(0, int(chain_depth or 0)),
                max(0, int(max_chain_depth)),
                expires_modifier,
            ),
        )
    except sqlite3.IntegrityError:
        existing = db.fetch_one(
            """SELECT request_id FROM spawn_requests
               WHERE run_id = ? AND dedup_key = ? AND status = 'pending'
               LIMIT 1""",
            (run_id, dedup_key),
        )
        if existing:
            return existing["request_id"]
        raise
    if commit:
        db.conn.commit()
    return req_id


def poll_spawn_requests(
    db,
    run_id: str = None,
    status: str = "pending",
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    Orchestrator 轮询：取出待处理的 spawn 请求。

    Args:
        db: SwarmDB 实例
        run_id: 指定 run（None = 所有 run）
        status: 筛选状态（默认 pending）
        limit: 最大返回数

    Returns:
        spawn 请求列表，按 priority DESC, created_at ASC 排序
    """
    if run_id:
        rows = db.fetch_all(
            """SELECT * FROM spawn_requests
               WHERE run_id = ? AND status = ?
                 AND expires_at > datetime('now')
               ORDER BY priority DESC, created_at ASC
               LIMIT ?""",
            (run_id, status, limit),
        )
    else:
        rows = db.fetch_all(
            """SELECT * FROM spawn_requests
               WHERE status = ?
                 AND expires_at > datetime('now')
               ORDER BY priority DESC, created_at ASC
               LIMIT ?""",
            (status, limit),
        )
    return [dict(r) for r in rows]


def claim_spawn_requests(
    db,
    run_id: str = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    原子领取 pending spawn 请求，将状态切到 spawning。

    多个 Orchestrator 并发时，SELECT 可能看到同一批候选，但每条请求只有一个
    UPDATE ... WHERE status='pending' 会成功。
    """
    candidates = poll_spawn_requests(db, run_id=run_id, status="pending", limit=limit)
    claimed: List[Dict[str, Any]] = []

    for req in candidates:
        claim_token = str(uuid.uuid4())
        cur = db.execute(
            """UPDATE spawn_requests
               SET status = 'spawning'
                   , claimed_at = datetime('now')
                   , claimed_by = ?
               WHERE request_id = ?
                 AND status = 'pending'
                 AND expires_at > datetime('now')""",
            (claim_token, req["request_id"]),
        )
        if cur.rowcount == 1:
            req["status"] = "spawning"
            req["claimed_by"] = claim_token
            req["claim_token"] = claim_token
            claimed.append(req)

    if claimed:
        db.conn.commit()
    return claimed


def recover_stale_spawn_claims(db, stale_seconds: int = 120) -> int:
    """
    将长时间停在 spawning 的请求释放回 pending。

    这覆盖 Orchestrator 在 claim 后崩溃/被杀的场景。过期请求仍由
    expire_old_requests() 处理。
    """
    stale = max(1, int(stale_seconds or 120))
    cur = db.execute(
        """UPDATE spawn_requests
           SET status = 'pending',
               claimed_at = NULL,
               claimed_by = NULL,
               reason = COALESCE(reason, '') || ' | recovered_stale_claim'
           WHERE status = 'spawning'
             AND expires_at > datetime('now')
             AND claimed_at IS NOT NULL
             AND (julianday('now') - julianday(claimed_at)) * 86400 > ?""",
        (stale,),
    )
    db.conn.commit()
    return cur.rowcount


def mark_spawn_fulfilled(
    db,
    request_id: str,
    spawned_agent_id: str,
    claim_token: str = None,
) -> bool:
    """标记 spawn 请求已完成；spawning 请求必须匹配 claim token。"""
    cur = db.execute(
        """UPDATE spawn_requests
           SET status = 'fulfilled',
               spawned_agent_id = ?,
               claimed_at = NULL,
               claimed_by = NULL
           WHERE request_id = ?
             AND (
               (status = 'pending' AND claimed_by IS NULL)
               OR (status = 'spawning' AND claimed_by = ?)
             )""",
        (spawned_agent_id, request_id, claim_token),
    )
    db.conn.commit()
    if cur.rowcount != 1:
        _log.warning("Spawn: rejected fulfillment for %s due to claim token mismatch", request_id)
        return False
    return True


def mark_spawn_rejected(db, request_id: str, reason: str = "") -> None:
    """标记 spawn 请求被拒绝"""
    extra = f" | rejected: {reason}" if reason else ""
    db.execute(
        "UPDATE spawn_requests SET status = 'rejected', "
        "claimed_at = NULL, claimed_by = NULL, reason = COALESCE(reason, '') || ? "
        "WHERE request_id = ? AND status IN ('pending', 'spawning')",
        (extra, request_id),
    )
    db.conn.commit()


def expire_old_requests(db) -> int:
    """
    将过期的 pending 请求标记为 expired。
    
    Returns:
        被标记为 expired 的记录数
    """
    cur = db.execute(
        "UPDATE spawn_requests SET status = 'expired', claimed_at = NULL, claimed_by = NULL "
        "WHERE status IN ('pending', 'spawning') AND expires_at < datetime('now')"
    )
    db.conn.commit()
    return cur.rowcount


def merge_duplicate_requests(db, run_id: str) -> int:
    """
    合并同一 run 中相同 dedup_key 的重复 pending 请求。
    
    策略：保留优先级最高的，其余标记为 rejected。
    防止 5 个 scanner 同时请求同一个 exploiter。

    Returns:
        被合并的请求数
    """
    rows = db.fetch_all(
        """SELECT request_id, run_id, requested_role, reason, context_entry_ids,
                  dedup_key, priority, created_at
           FROM spawn_requests
           WHERE run_id = ? AND status = 'pending'
             AND expires_at > datetime('now')""",
        (run_id,),
    )

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        req = dict(row)
        dedup_key = req.get("dedup_key") or build_spawn_dedup_key(
            req["run_id"],
            req["requested_role"],
            req["reason"],
            _decode_context_entry_ids(req.get("context_entry_ids")),
        )
        req["dedup_key"] = dedup_key
        groups.setdefault(dedup_key, []).append(req)

    merged = 0
    backfilled = 0
    for dedup_key, requests in groups.items():
        if len(requests) <= 1:
            req = requests[0]
            current_key = next(
                (dict(row).get("dedup_key") for row in rows if row["request_id"] == req["request_id"]),
                None,
            )
            if not current_key:
                db.execute(
                    "UPDATE spawn_requests SET dedup_key = ? WHERE request_id = ?",
                    (dedup_key, req["request_id"]),
                )
                backfilled += 1
            continue

        keep = sorted(
            requests,
            key=lambda req: (-(req.get("priority") or 0), req.get("created_at") or ""),
        )[0]
        keep_id = keep["request_id"]
        duplicate_ids = [req["request_id"] for req in requests if req["request_id"] != keep_id]
        placeholders = ",".join("?" for _ in duplicate_ids)
        cur = db.execute(
            f"""UPDATE spawn_requests
                SET status = 'rejected', dedup_key = ?
                WHERE request_id IN ({placeholders})""",
            (dedup_key, *duplicate_ids),
        )
        db.execute(
            "UPDATE spawn_requests SET dedup_key = ? WHERE request_id = ?",
            (dedup_key, keep_id),
        )
        merged += cur.rowcount

    if merged or backfilled:
        db.conn.commit()
    return merged
