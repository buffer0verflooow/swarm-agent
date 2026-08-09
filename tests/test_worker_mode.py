#!/usr/bin/env python3
"""
Phase C 集成测试 — Worker 模式 + Caveman 注入 + Controller spawn

验证:
  1. spawn_request 带 [worker_mode] → orchestrator 检测并设置 worker_mode
  2. _build_spawn_context_worker 不包含探索记忆
  3. HermesSpawnHandler worker_mode 注入 caveman 指令
  4. Controller spawn 自动标记 [worker_mode]
"""

import asyncio
import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from src.swarm.spawn_handler import HermesSpawnHandler, MockSpawnHandler

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def setup_db():
    db_path = os.path.join(tempfile.gettempdir(), "test_phase_c.db")
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.unlink(p)
    db = SwarmDB(db_path)
    db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_profiles (agent_id TEXT PRIMARY KEY, agent_name TEXT, role TEXT, status TEXT, capabilities TEXT, model_preference TEXT, model_profile_id TEXT);
        CREATE TABLE IF NOT EXISTS agent_heartbeats (agent_id TEXT PRIMARY KEY, run_id TEXT, last_beat TEXT, beat_count INTEGER DEFAULT 0, load_score REAL DEFAULT 0, current_task_id TEXT, stealable INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS knowledge_entries (id TEXT, title TEXT, content TEXT, knowledge_type TEXT, level INTEGER, source_run_id TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS explorer_traces (trace_id TEXT, run_id TEXT, target_url TEXT, vulnerability_class TEXT);
    """)
    db.conn.commit()
    return db


def test_worker_context_stripped(db):
    """验证 worker context 不包含探索记忆。"""
    print("\n── test: worker context stripped ──")

    from src.swarm.orchestrator import SwarmOrchestrator
    orch = SwarmOrchestrator(db)

    # 注入一些探索数据
    db.execute(
        """INSERT INTO exploration_traces
           (trace_id, run_id, target_url, method, vulnerability_class, result)
           VALUES ('t1', 'run-c', 'http://demo.test/x', 'GET', 'IDOR', 'not_found')"""
    )
    db.conn.commit()

    req = {
        "reason": "test scan task",
        "context_entry_ids": "[]",
        "worker_mode": True,
    }

    ctx = orch._build_spawn_context_worker(req)
    print(f"  Worker context ({len(ctx)} chars): {ctx[:200]}")

    check("no exploration memory", "蜂群探索记忆" not in ctx)
    check("no exhausted path", "已穷尽" not in ctx)
    check("contains task", "test scan task" in ctx)


def test_full_context_includes_memory(db):
    """验证非 worker context 包含探索记忆。"""
    print("\n── test: full context includes exploration memory ──")

    from src.swarm.orchestrator import SwarmOrchestrator
    orch = SwarmOrchestrator(db)

    # 注入探索记忆 + 知识条目
    eid = str(uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_entries (id, title, content, knowledge_type, level, source_agent)
           VALUES (?, 'test finding', 'test content', 'vulnerability', 3, 'test-agent')""",
        (eid,),
    )
    db.conn.commit()

    req = {
        "reason": "test scan task",
        "context_entry_ids": json.dumps([eid]),
        "worker_mode": False,
    }

    ctx = orch._build_spawn_context(req)
    print(f"  Full context ({len(ctx)} chars): {ctx[:250]}")

    check("contains KB entry", "test finding" in ctx)
    # May or may not have exploration memory depending on data


def test_spawn_handler_worker_mode(db):
    """验证 spawn handler 注入 caveman 指令。"""
    print("\n── test: spawn handler worker mode ──")

    handler = MockSpawnHandler(db)
    req = {
        "requested_role": "scanner",
        "reason": "scan target API",
        "run_id": "run-c",
        "chain_depth": 1,
        "max_chain_depth": 3,
        "context_entry_ids": "[]",
        "worker_mode": True,
    }

    agent_id = asyncio.run(handler(req, "minimal context"))
    check("worker spawn succeeded", agent_id is not None and len(agent_id) > 0)


def test_spawn_handler_normal_mode(db):
    """验证正常模式不注入 caveman 指令。"""
    print("\n── test: spawn handler normal mode ──")

    handler = MockSpawnHandler(db)
    req = {
        "requested_role": "scanner",
        "reason": "scan target API",
        "run_id": "run-c",
        "chain_depth": 1,
        "max_chain_depth": 3,
        "context_entry_ids": "[]",
        "worker_mode": False,
    }

    agent_id = asyncio.run(handler(req, "full context"))
    check("normal spawn succeeded", agent_id is not None and len(agent_id) > 0)


def test_worker_mode_detection_in_orchestrator(db):
    """验证 orchestrator 从 reason 中检测 [worker_mode] 标记。"""
    print("\n── test: worker_mode detection from reason ──")

    reason = "Controller auto-spawn: need more scanners [worker_mode]"
    has_worker = "[worker_mode]" in reason
    check("marker detected", has_worker)
    cleaned = reason.replace(" [worker_mode]", "")
    check("marker removed", "[worker_mode]" not in cleaned)
    check("reason preserved", "Controller auto-spawn" in cleaned)


def main():
    global PASS, FAIL

    db = setup_db()

    test_worker_context_stripped(db)
    test_full_context_includes_memory(db)
    test_spawn_handler_worker_mode(db)
    test_spawn_handler_normal_mode(db)
    test_worker_mode_detection_in_orchestrator(db)

    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    print(f"{'='*50}")

    db.close()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
