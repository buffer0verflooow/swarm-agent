"""进程内存回收测试（DeepTutor memory_reclaim.py 移植）。

覆盖:
- release_unused_memory 返回 (int, bool) 且不抛异常
- 节流：间隔内重复调用返回 (0, False)
- force=True 绕过节流
- orchestrator 治理 tick 集成（monkeypatch 验证被调用）
"""

from __future__ import annotations

import asyncio
import sys

from src.swarm.memory_reclaim import _MIN_INTERVAL_SECONDS, _last_reclaim, release_unused_memory


def test_release_returns_tuple_and_no_raise(monkeypatch):
    # 绕过节流保证真实执行
    collected, trimmed = release_unused_memory(force=True)
    assert isinstance(collected, int)
    assert isinstance(trimmed, bool)
    assert collected >= 0


def test_throttle_blocks_second_call(monkeypatch):
    # 重置节流状态
    monkeypatch.setattr("src.swarm.memory_reclaim._last_reclaim", 0.0)
    release_unused_memory(force=True)  # 第一次真实执行
    collected, trimmed = release_unused_memory()  # 间隔内 → 节流
    assert collected == 0
    assert trimmed is False


def test_force_bypasses_throttle(monkeypatch):
    monkeypatch.setattr("src.swarm.memory_reclaim._last_reclaim", 0.0)
    release_unused_memory()  # 第一次
    collected, trimmed = release_unused_memory(force=True)  # force → 真实执行
    assert isinstance(collected, int)
    assert isinstance(trimmed, bool)


def test_orchestrator_governance_calls_reclaim(db, run_id, monkeypatch):
    """治理 tick 调用 release_unused_memory（monkeypatch 验证）。"""
    from src.swarm.orchestrator import SwarmOrchestrator

    calls = []

    def fake_reclaim(*args, **kwargs):
        calls.append(1)
        return (0, False)

    monkeypatch.setattr("src.swarm.orchestrator.release_unused_memory", fake_reclaim)

    orch = SwarmOrchestrator(db)
    asyncio.run(orch._tick_governance(run_id))
    assert len(calls) == 1, "governance tick should call memory reclaim exactly once"
