"""
Spawn 请求机制：子 Agent 写信号 → Orchestrator 异步实例化

这是 stigmergy 模式的核心——Agent 不直接生成子 Agent，而是
在 KB 中留下"spawn 信号"，Orchestrator 轮询检测并统一执行。

单层嵌套限制的解决方案：
  虽然框架禁止子 Agent 直接 spawn 孙 Agent，但通过 KB 中转，
  逻辑上的"孙代"关系得以保留（通过 parent_task_id + context_entry_ids）。

用法:
    from src.swarm.spawner import request_spawn, poll_spawn_requests, mark_spawn_fulfilled

    # Agent 端：发现需要协作
    request_spawn(db, run_id, "scanner-001", "exploiter",
                  reason="发现 SQL injection 漏洞", 
                  context_entry_ids=[entry_id])

    # Orchestrator 端：轮询处理
    pending = poll_spawn_requests(db, run_id)
    for req in pending:
        agent = spawn_agent(...)
        mark_spawn_fulfilled(db, req["request_id"], agent.id)
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional


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
    Returns:
        request_id
    """
    req_id = str(uuid.uuid4())
    
    # 如果未指定 max_chain_depth, 使用系统默认
    if max_chain_depth is None:
        max_chain_depth = 3  # MAX_CHAIN_DEPTH_DEFAULT

    db.execute(
        """INSERT INTO spawn_requests
           (request_id, run_id, requesting_agent, parent_task_id,
            requested_role, reason, context_entry_ids, priority, chain_depth, max_chain_depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            req_id, run_id, requesting_agent, parent_task_id,
            requested_role, reason,
            json.dumps(context_entry_ids or []),
            priority, chain_depth, max_chain_depth,
        ),
    )
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


def mark_spawn_fulfilled(db, request_id: str, spawned_agent_id: str) -> None:
    """标记 spawn 请求已完成"""
    db.execute(
        """UPDATE spawn_requests
           SET status = 'fulfilled', spawned_agent_id = ?
           WHERE request_id = ?""",
        (spawned_agent_id, request_id),
    )
    db.conn.commit()


def mark_spawn_rejected(db, request_id: str, reason: str = "") -> None:
    """标记 spawn 请求被拒绝"""
    extra = f" | rejected: {reason}" if reason else ""
    db.execute(
        "UPDATE spawn_requests SET status = 'rejected', "
        "reason = COALESCE(reason, '') || ? WHERE request_id = ?",
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
        "UPDATE spawn_requests SET status = 'expired' "
        "WHERE status = 'pending' AND expires_at < datetime('now')"
    )
    db.conn.commit()
    return cur.rowcount


def merge_duplicate_requests(db, run_id: str) -> int:
    """
    合并同一 run 中相同 requested_role 的重复 pending 请求。
    
    策略：保留优先级最高的，其余标记为 rejected。
    防止 5 个 scanner 同时请求同一个 exploiter。

    Returns:
        被合并的请求数
    """
    # 找到重复的 (run_id, requested_role) 组
    dupes = db.fetch_all(
        """SELECT run_id, requested_role, COUNT(*) AS cnt,
                  MAX(priority) AS max_priority
           FROM spawn_requests
           WHERE run_id = ? AND status = 'pending'
             AND expires_at > datetime('now')
           GROUP BY run_id, requested_role
           HAVING cnt > 1""",
        (run_id,),
    )

    merged = 0
    for row in dupes:
        # 保留优先级最高的一条
        keep = db.fetch_one(
            """SELECT request_id FROM spawn_requests
               WHERE run_id = ? AND requested_role = ? AND status = 'pending'
                 AND priority = ?
               ORDER BY created_at ASC LIMIT 1""",
            (row["run_id"], row["requested_role"], row["max_priority"]),
        )
        if not keep:
            continue

        keep_id = keep["request_id"]
        cur = db.execute(
            """UPDATE spawn_requests SET status = 'rejected'
               WHERE run_id = ? AND requested_role = ? AND status = 'pending'
                 AND request_id != ?""",
            (row["run_id"], row["requested_role"], keep_id),
        )
        merged += cur.rowcount

    if merged:
        db.conn.commit()
    return merged
