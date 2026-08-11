"""执行配额测试（DeepTutor quota.py 移植）。

覆盖:
- ExecutionQuota 并发上限 / 速率窗口 / 窗口过期恢复
- orchestrator 集成：默认配额、配额耗尽时 _tick_spawn 不 spawn
"""

from __future__ import annotations

import asyncio
import time

from src.swarm.orchestrator import SwarmOrchestrator
from src.swarm.quota import ExecutionQuota
from src.swarm.spawn_handler import MockSpawnHandler
from src.swarm.spawner import request_spawn, poll_spawn_requests


def test_concurrency_cap():
    quota = ExecutionQuota(max_concurrent=2, max_per_minute=100)
    assert quota.can_acquire() is True

    async def main():
        await quota.acquire()
        await quota.acquire()
        return quota.can_acquire()

    assert asyncio.run(main()) is False
    quota.release()
    assert quota.can_acquire() is True


def test_rate_window():
    quota = ExecutionQuota(max_concurrent=100, max_per_minute=2)

    async def main():
        await quota.acquire()
        quota.release()
        await quota.acquire()
        quota.release()
        return quota.can_acquire()

    assert asyncio.run(main()) is False
    # 手动把时间戳拨到窗口外 → 恢复
    now = time.monotonic()
    quota._recent = __import__("collections").deque([now - quota.window_seconds - 1])
    assert quota.can_acquire() is True


def test_pending_in_window():
    quota = ExecutionQuota(max_concurrent=10, max_per_minute=10)

    async def main():
        await quota.acquire()
        await quota.acquire()
        return quota.pending_in_window()

    assert asyncio.run(main()) == 2


def test_orchestrator_default_quota(db):
    orch = SwarmOrchestrator(db)
    assert orch.quota.max_concurrent == 4
    assert orch.quota.max_per_minute == 8


def test_tick_spawn_skips_when_quota_exhausted(db, run_id):
    """配额耗尽时 _tick_spawn 不 spawn，请求保持 pending。"""
    orch = SwarmOrchestrator(db)
    orch.set_spawn_handler(MockSpawnHandler(db))

    # 插一条 pending spawn 请求
    req_id = request_spawn(
        db, run_id,
        requesting_agent="agent-a",
        requested_role="scanner",
        reason="quota test",
    )
    assert req_id

    # 配额耗尽：并发槽占满（acquire 2 次，max_concurrent=4 → 用速率窗口堵住更直接：
    # max_per_minute 默认 8，先手动把 _recent 塞满窗口）
    now = time.monotonic()
    for _ in range(orch.quota.max_per_minute):
        orch.quota._recent.append(now)
    assert orch.quota.can_acquire() is False

    async def main():
        await orch._tick_spawn(run_id)

    asyncio.run(main())

    # spawn 请求未被 claim（保持 pending）
    pending = poll_spawn_requests(db, run_id)
    assert any(r["request_id"] == req_id for r in pending), (
        "request should remain pending when quota exhausted"
    )


def test_tick_spawn_acquires_quota_on_success(db, run_id):
    """spawn 成功 → 配额记录一次速率单位。"""
    orch = SwarmOrchestrator(db, max_concurrent_spawns=4, max_spawns_per_minute=8)
    orch.set_spawn_handler(MockSpawnHandler(db))

    req_id = request_spawn(
        db, run_id,
        requesting_agent="agent-a",
        requested_role="scanner",
        reason="quota success test",
    )
    assert req_id

    async def main():
        await orch._tick_spawn(run_id)

    asyncio.run(main())

    assert orch.quota.pending_in_window() == 1
    # 请求被履行
    row = db.fetch_one(
        "SELECT status FROM spawn_requests WHERE request_id = ?", (req_id,)
    )
    assert row["status"] == "fulfilled"
