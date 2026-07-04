"""
Agent 生命周期管理：注册 → 心跳 → 超时清理

每个 Agent 在启动时注册到 swarm_run，运行期间定期发送心跳，
Orchestrator 定期扫描超时 Agent 并清理僵尸任务。

用法:
    from src.swarm.lifecycle import AgentLifecycle, cleanup_stale_agents

    # Agent 端
    lc = AgentLifecycle(db, agent_id="scanner-001", run_id=run_id)
    lc.register(role="scanner", capabilities=["nmap", "nuclei"])
    while running:
        lc.beat(current_task_id=task_id, load=0.5)
        time.sleep(30)
    lc.deregister()

    # Orchestrator 端 (每 10s)
    stale = cleanup_stale_agents(db)
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

_log = logging.getLogger("swarm_knowledge.lifecycle")

HEARTBEAT_INTERVAL_SEC = 30
TIMEOUT_MULTIPLIER = 3  # 超过 3x interval (90s) 视为超时
DEFAULT_TIMEOUT_SEC = HEARTBEAT_INTERVAL_SEC * TIMEOUT_MULTIPLIER


class AgentLifecycle:
    """单 Agent 的生命周期管理器"""

    def __init__(self, db, agent_id: str, run_id: str):
        self.db = db
        self.agent_id = agent_id
        self.run_id = run_id

    def register(self, role: str, capabilities: list = None, model: str = None) -> None:
        """
        Agent 启动时调用一次。
        写入 agent_profiles（如果已存在则更新状态）并插入心跳记录。
        beat_count 不会在重连时重置——使用 UPSERT 保留历史计数。
        """
        caps = json.dumps(capabilities or [])

        # Upsert agent profile
        self.db.execute(
            """INSERT INTO agent_profiles
               (agent_id, agent_name, role, capabilities, model_preference, status)
               VALUES (?, ?, ?, ?, ?, 'active')
               ON CONFLICT(agent_id) DO UPDATE SET
                   status = 'active',
                   role = excluded.role,
                   capabilities = excluded.capabilities,
                   model_preference = excluded.model_preference""",
            (self.agent_id, f"{role}-{self.agent_id[:8]}", role, caps, model),
        )

        # UPSERT heartbeat: 不重置 beat_count（保留历史）
        self.db.execute(
            """INSERT INTO agent_heartbeats
               (agent_id, run_id, last_beat, beat_count)
               VALUES (?, ?, datetime('now'), 1)
               ON CONFLICT(agent_id) DO UPDATE SET
                   last_beat = datetime('now'),
                   run_id = excluded.run_id""",
            (self.agent_id, self.run_id),
        )
        self.db.conn.commit()

    def beat(self, current_task_id: str = None, load: float = 0.0) -> None:
        """
        每 HEARTBEAT_INTERVAL_SEC 秒调用，证明自己还活着。
        
        Args:
            current_task_id: 当前正在执行的任务 ID
            load: 0.0~1.0 负载分数，用于工作窃取调度

        Raises:
            RuntimeError: 心跳记录已被清理（Agent 可能已超时）
        """
        if not (0.0 <= load <= 1.0):
            _log.warning("beat: load=%.2f out of range [0,1], clamping", load)
            load = max(0.0, min(1.0, load))

        cur = self.db.execute(
            """UPDATE agent_heartbeats
               SET last_beat = datetime('now'),
                   beat_count = beat_count + 1,
                   current_task_id = ?,
                   load_score = ?
               WHERE agent_id = ?""",
            (current_task_id, load, self.agent_id),
        )
        self.db.conn.commit()

        if cur.rowcount == 0:
            raise RuntimeError(
                f"Agent {self.agent_id} 心跳记录不存在，可能已超时被 Orchestrator 清理"
            )

    def deregister(self) -> None:
        """Agent 正常退出时调用：标记 idle + 删除心跳（同一事务中）"""
        try:
            self.db.execute(
                "UPDATE agent_profiles SET status = 'idle' WHERE agent_id = ?",
                (self.agent_id,),
            )
            self.db.execute(
                "DELETE FROM agent_heartbeats WHERE agent_id = ?",
                (self.agent_id,),
            )
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise


def cleanup_stale_agents(db, timeout_sec: int = None) -> List[str]:
    """
    Orchestrator 定期调用，清理僵尸 Agent。
    
    超过 timeout_sec 无心跳的 Agent：
    1. 删除心跳记录（作为"声索"，防止竞态）
    2. 取消其正在执行的 task → timeout
    3. 标记 agent_profiles 为 deprecated

    整个过程在单个事务中执行，保证原子性。

    Args:
        db: SwarmDB 实例
        timeout_sec: 超时秒数，默认 DEFAULT_TIMEOUT_SEC (90s)

    Returns:
        被清理的 agent_id 列表
    """
    timeout = timeout_sec if timeout_sec is not None else DEFAULT_TIMEOUT_SEC

    with db.transaction():
        stale = db.fetch_all(
            """SELECT ah.agent_id, ah.current_task_id
               FROM agent_heartbeats ah
               WHERE (julianday('now') - julianday(ah.last_beat)) * 86400 > ?""",
            (timeout,),
        )

        cleaned = []
        for row in stale:
            agent_id = row["agent_id"]

            # 先删除心跳 — 防止 beat() 在清理间隙重写
            db.execute(
                "DELETE FROM agent_heartbeats WHERE agent_id = ?", (agent_id,)
            )

            # 取消正在执行的任务
            if row["current_task_id"]:
                db.execute(
                    """UPDATE agent_tasks
                       SET status = 'timeout', updated_at = datetime('now')
                       WHERE task_id = ? AND status = 'running'""",
                    (row["current_task_id"],),
                )

            # 标记 agent 为废弃
            db.execute(
                "UPDATE agent_profiles SET status = 'deprecated' WHERE agent_id = ?",
                (agent_id,),
            )
            cleaned.append(agent_id)

        return cleaned


def get_live_agents(db, run_id: str, max_idle_sec: int = None) -> list:
    """
    获取指定 run 中当前存活的 Agent 列表（含负载信息）。

    Args:
        db: SwarmDB 实例
        run_id: 蜂群运行 ID
        max_idle_sec: 心跳最大空闲秒数，默认 DEFAULT_TIMEOUT_SEC

    Returns:
        [{"agent_id": ..., "role": ..., "load_score": ..., "current_task_id": ...}, ...]
    """
    idle = max_idle_sec if max_idle_sec is not None else DEFAULT_TIMEOUT_SEC
    rows = db.fetch_all(
        """SELECT ah.agent_id, ap.role, ah.load_score, ah.current_task_id, ah.last_beat
           FROM agent_heartbeats ah
           JOIN agent_profiles ap ON ah.agent_id = ap.agent_id
           WHERE ah.run_id = ?
             AND (julianday('now') - julianday(ah.last_beat)) * 86400 < ?
           ORDER BY ah.load_score ASC""",
        (run_id, idle),
    )
    return [dict(r) for r in rows]
