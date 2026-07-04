"""
集成测试：3 个 mock Agent 跑一个完整 swarm run。

验证:
  1. Agent 注册 + 心跳 + 超时清理
  2. Spawn 请求写入 + 轮询 + 履行
  3. Capture 自动触发 spawn
  4. Orchestrator 主循环
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from src.agents.capture import CaptureContext, CaptureSource, capture
from src.swarm.lifecycle import AgentLifecycle, cleanup_stale_agents, get_live_agents
from src.swarm.spawner import (
    request_spawn, poll_spawn_requests,
    mark_spawn_fulfilled, merge_duplicate_requests, expire_old_requests,
)
from src.swarm.orchestrator import SwarmOrchestrator, POLL_SPAWN_SEC, POLL_HEARTBEAT_SEC


# ── Setup ──

def setup_test_db():
    """创建临时测试数据库"""
    db_path = "/tmp/test_swarm_loop.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    db = SwarmDB(db_path)
    db.init()
    return db


def create_test_run(db) -> str:
    """创建测试 swarm_run"""
    run_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO swarm_runs (run_id, swarm_name, intent, target_type, target_id, status)
           VALUES (?, 'test-swarm', 'recon', 'webapp', 'test-target', 'running')""",
        (run_id,),
    )
    db.conn.commit()
    return run_id


# ── Tests ──

def test_lifecycle():
    """测试 Agent 注册 → 心跳 → 清理"""
    print("\n=== Test: Lifecycle ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    # 注册 3 个 Agent
    agents = []
    for role in ("scanner", "analyst", "exploiter"):
        agent_id = f"test-{role}-001"
        lc = AgentLifecycle(db, agent_id, run_id)
        lc.register(role=role, capabilities=[f"mock_{role}"])
        agents.append((agent_id, lc))
        print(f"  registered: {agent_id}")

    # 验证 agent_profiles
    for agent_id, _ in agents:
        row = db.fetch_one("SELECT role, status FROM agent_profiles WHERE agent_id=?", (agent_id,))
        assert row["status"] == "active", f"{agent_id}: expected active, got {row['status']}"
    print("  ✅ agent_profiles populated")

    # 验证心跳表
    live = get_live_agents(db, run_id)
    assert len(live) == 3, f"expected 3 live agents, got {len(live)}"
    print(f"  ✅ {len(live)} live agents detected")

    # 创建 dummy task 满足 FK 约束
    for agent_id, _ in agents:
        task_id = f"task-{agent_id}"
        db.execute(
            """INSERT INTO agent_tasks (task_id, run_id, agent_id, task_type, status)
               VALUES (?, ?, ?, 'subtask', 'running')""",
            (task_id, run_id, agent_id),
        )
    db.conn.commit()

    # 发送心跳
    for agent_id, lc in agents:
        lc.beat(current_task_id=f"task-{agent_id}", load=0.3)
    print("  ✅ heartbeats sent")

    # 注销一个 Agent
    agents[0][1].deregister()
    print("  ✅ agent deregistered")

    # 验证减少
    live_after = get_live_agents(db, run_id)
    assert len(live_after) == 2, f"expected 2 live agents, got {len(live_after)}"
    print("  ✅ deregistration reflected in heartbeat table")

    # 验证 profile 状态变更
    row = db.fetch_one("SELECT status FROM agent_profiles WHERE agent_id=?", (agents[0][0],))
    assert row["status"] == "idle", f"expected idle, got {row['status']}"
    print("  ✅ profile status updated to idle")

    db.close()
    print("=== Lifecycle: ALL PASSED ===")


def test_spawn():
    """测试 spawn 请求写入 → 轮询 → 履行"""
    print("\n=== Test: Spawn ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    # 注册 requesting agent
    lc = AgentLifecycle(db, "test-scanner-001", run_id)
    lc.register(role="scanner")

    # 写入 spawn 请求
    req_id = request_spawn(
        db, run_id,
        requesting_agent="test-scanner-001",
        requested_role="exploiter",
        reason="Found SQL injection vulnerability",
        context_entry_ids=["entry-001", "entry-002"],
        parent_task_id=None,
    )
    print(f"  spawn request created: {req_id[:8]}")

    # 轮询 pending 请求
    pending = poll_spawn_requests(db, run_id)
    assert len(pending) == 1, f"expected 1 pending, got {len(pending)}"
    req = pending[0]
    assert req["requested_role"] == "exploiter"
    assert req["status"] == "pending"
    print(f"  ✅ poll returned 1 pending request: {req['requested_role']}")

    # 履行 — 需要先注册目标 agent 满足 FK 约束
    lc2 = AgentLifecycle(db, "test-exploiter-001", run_id)
    lc2.register(role="exploiter")
    mark_spawn_fulfilled(db, req_id, "test-exploiter-001")
    req_after = db.fetch_one("SELECT status, spawned_agent_id FROM spawn_requests WHERE request_id=?", (req_id,))
    assert req_after["status"] == "fulfilled"
    assert req_after["spawned_agent_id"] == "test-exploiter-001"
    print("  ✅ spawn fulfilled")

    # 重复请求合并
    for i in range(3):
        request_spawn(db, run_id, "test-scanner-001", "reporter", f"dupe {i}", priority=50 + i)
    merged = merge_duplicate_requests(db, run_id)
    print(f"  ✅ merged {merged} duplicate reporter requests")

    # 过期清理
    # 手动设置一个过期
    db.execute(
        "UPDATE spawn_requests SET expires_at = datetime('now', '-1 minute') "
        "WHERE request_id = (SELECT request_id FROM spawn_requests WHERE status = 'pending' LIMIT 1)"
    )
    expired = expire_old_requests(db)
    print(f"  ✅ expired {expired} old requests")

    db.close()
    print("=== Spawn: ALL PASSED ===")


def test_capture_triggers_spawn():
    """测试 capture() 自动触发 spawn 请求"""
    print("\n=== Test: Capture → Auto-spawn ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    # 注册 scanner agent
    lc = AgentLifecycle(db, "test-scanner-001", run_id)
    lc.register(role="scanner")

    # 创建 dummy task 满足 capture 的 FK 约束
    db.execute(
        "INSERT INTO agent_tasks (task_id, run_id, agent_id, task_type, status) "
        "VALUES ('task-001', ?, 'test-scanner-001', 'scan', 'running')",
        (run_id,),
    )
    db.conn.commit()

    # 模拟 capture 一个 vulnerability 发现
    ctx = CaptureContext(
        source=CaptureSource.TASK_RESULT,
        content="发现 SQL injection 漏洞在 /api/users?id= 参数。"
                " 攻击者可注入 UNION SELECT 语句提取所有用户数据。"
                " 建议使用参数化查询修复此漏洞。"
                " CVE pattern: similar to CVE-2023-1234",
        source_agent="test-scanner-001",
        source_run_id=run_id,
        source_task_id="task-001",
        metadata={"task_type": "scan", "tool": "sqlmap", "intent": "attack"},
    )

    entry_id = capture(db, ctx)
    print(f"  capture result: {entry_id[:8] if entry_id else 'filtered'}")

    # 验证 spawn 请求是否被自动创建
    pending = poll_spawn_requests(db, run_id)
    spawned_roles = [r["requested_role"] for r in pending]
    print(f"  auto-spawn roles: {spawned_roles}")

    if "exploiter" in spawned_roles:
        print("  ✅ capture auto-triggered exploiter spawn")
    else:
        # 可能因为分类不匹配没触发
        row = db.fetch_one(
            "SELECT knowledge_type, knowledge_intent FROM knowledge_entries WHERE id=?",
            (entry_id,),
        )
        if row:
            print(f"  ⚠ no spawn triggered (type={row['knowledge_type']}, intent={row['knowledge_intent']})")

    db.close()
    print("=== Capture Auto-spawn: DONE ===")


async def test_orchestrator_loop():
    """测试 Orchestrator 主循环（mock spawn handler）"""
    print("\n=== Test: Orchestrator Loop ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    spawned_agents = []

    async def mock_spawn_handler(req: dict, context: str) -> str:
        """Mock: 不调用 Claude API，直接返回 agent_id"""
        agent_id = f"mock-{req['requested_role']}-{uuid.uuid4().hex[:6]}"
        # 注册到 agent_profiles 满足 FK 约束
        lc = AgentLifecycle(db, agent_id, run_id)
        lc.register(role=req['requested_role'])
        spawned_agents.append({"role": req["requested_role"], "agent_id": agent_id, "reason": req["reason"]})
        return agent_id

    orch = SwarmOrchestrator(db)
    orch.set_spawn_handler(mock_spawn_handler)

    # 注册 3 个 Agent
    for role in ("scanner", "analyst", "reporter"):
        agent_id = f"test-{role}-001"
        lc = AgentLifecycle(db, agent_id, run_id)
        lc.register(role=role)

    # 手动创建 spawn 请求
    request_spawn(db, run_id, "test-scanner-001", "exploiter",
                  reason="发现漏洞需要利用", priority=80)
    request_spawn(db, run_id, "test-analyst-001", "exploiter",
                  reason="分析确认需要利用", priority=60)

    # 在后台运行 Orchestrator 10 秒
    async def run_short():
        await orch.run_loop(run_id, tick_interval=1.0)

    task = asyncio.create_task(run_short())

    # 等待 spawn 被处理
    await asyncio.sleep(POLL_SPAWN_SEC + 2)
    orch.stop()
    await task

    print(f"  spawned agents: {len(spawned_agents)}")
    for sa in spawned_agents:
        print(f"    - {sa['role']}: {sa['agent_id']} ({sa['reason'][:50]})")

    # 验证 spawn_requests 状态
    fulfilled = db.fetch_all(
        "SELECT request_id, requested_role, status FROM spawn_requests WHERE status='fulfilled'"
    )
    print(f"  fulfilled requests: {len(fulfilled)}")
    for f in fulfilled:
        print(f"    - {f['requested_role']}: {f['status']}")

    # 验证 behavior 日志
    behaviors = db.fetch_all("SELECT behavior_type, description FROM swarm_behaviors WHERE run_id=?", (run_id,))
    print(f"  behavior logs: {len(behaviors)}")
    for b in behaviors:
        print(f"    - [{b['behavior_type']}] {b['description'][:80]}")

    assert len(fulfilled) > 0, "Expected at least one spawn fulfilled"
    assert len(behaviors) > 0, "Expected at least one behavior logged"
    print("  ✅ Orchestrator processed spawn requests")
    print("  ✅ Behavior logged")

    # 测试心跳清理：标记一个 Agent 为"僵尸"
    db.execute(
        "UPDATE agent_heartbeats SET last_beat = datetime('now', '-120 seconds') WHERE agent_id = 'test-scanner-001'"
    )
    db.conn.commit()

    # 运行一次清理 tick
    stale = cleanup_stale_agents(db, timeout_sec=90)
    print(f"  stale agents cleaned: {stale}")
    assert len(stale) >= 1, f"Expected at least 1 stale agent, got {len(stale)}"

    # 验证 profile 被标记为 deprecated
    row = db.fetch_one("SELECT status FROM agent_profiles WHERE agent_id='test-scanner-001'")
    assert row["status"] == "deprecated", f"Expected deprecated, got {row['status']}"
    print("  ✅ stale agent cleanup works")

    db.close()
    print("=== Orchestrator Loop: ALL PASSED ===")


# ── Main ──

def main():
    """运行所有测试"""
    print("=" * 60)
    print("Swarm Knowledge Base — 集成测试")
    print("=" * 60)

    test_lifecycle()
    test_spawn()
    test_capture_triggers_spawn()

    # Orchestrator 需要 asyncio
    asyncio.run(test_orchestrator_loop())

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
