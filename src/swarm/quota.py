"""执行配额 — 并发上限 + 60 秒滑动窗口速率限制。

移植自 DeepTutor services/sandbox/quota.py 的设计：
防跑飞会话与突发请求（对应 Cloudflare 429 跨 Phase 传染问题）。

语义:
- acquire() 成功 = 发起一次执行：记录开始时间戳进滑动窗口（统计"发起次数"）
- release() = 执行结束：只释放并发槽（统计"在飞数量"）
- 速率窗口统计发起次数，并发统计在飞数量 — 两个正交维度
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import time
from typing import Deque


class QuotaExceeded(Exception):
    """超过并发或速率配额。"""


@dataclass
class ExecutionQuota:
    """每 run 执行配额守卫。

    Attributes:
        max_concurrent: 同时允许的在飞执行数（信号量）
        max_per_minute: 60 秒滑动窗口内最多发起的执行数
        window_seconds: 滑动窗口长度（默认 60）

    配置约束：max_per_minute 应 >= max_concurrent。若窗口速率小于并发上限，
    同一 tick 内连续 acquire 会阻塞等待窗口滑动（orchestrator 的
    _tick_spawn 单次最多 claim max_concurrent 个请求）。
    """

    max_concurrent: int = 4
    max_per_minute: int = 8
    window_seconds: float = 60.0

    _sem: asyncio.Semaphore = field(init=False)
    _recent: Deque[float] = field(init=False, default_factory=deque)
    _lock: asyncio.Lock = field(init=False)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(max(1, self.max_concurrent))
        self._recent = deque()
        self._lock = asyncio.Lock()

    def can_acquire(self) -> bool:
        """非阻塞检查：并发有余量 且 窗口内次数未达上限。"""
        if self._sem.locked():
            return False
        if self._count_recent() >= self.max_per_minute:
            return False
        return True

    async def acquire(self) -> None:
        """等待直到可发起一次执行，然后记录时间戳。

        并发不足时等信号量；速率超窗时释放槽并等窗口滑动。
        """
        while True:
            await self._sem.acquire()  # 阻塞直到拿到并发槽
            if self._count_recent() < self.max_per_minute:
                break
            # 速率超限：释放槽并等窗口滑动到最早记录过期
            self._sem.release()
            await asyncio.sleep(0.5)

        async with self._lock:
            self._recent.append(time.monotonic())

    def release(self) -> None:
        """释放一个并发槽（执行结束）。"""
        self._sem.release()

    def _count_recent(self) -> int:
        """清理过期时间戳后返回窗口内数量。"""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        return len(self._recent)

    def pending_in_window(self) -> int:
        """当前窗口内的发起次数（不清理）。"""
        return self._count_recent()

    def reset(self) -> None:
        """清空速率窗口（不重置并发槽计数）。"""
        self._recent.clear()
