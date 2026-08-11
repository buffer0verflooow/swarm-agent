"""进程内存回收 — 长驻进程空闲堆页归还 OS。

移植自 DeepTutor runtime/memory_reclaim.py 的设计：
Python 通常保留已释放的 arenas 供复用（快，但 RSS 看起来永久钉住）。
gc.collect() 回收循环引用；Linux 上 malloc_trim(0) 请求 glibc 归还空闲堆页。
非 Linux 平台只做 portable 的 cycle collection。

内置节流：两次实际回收之间最短间隔 _MIN_INTERVAL_SECONDS，防高频调用空转。
"""

from __future__ import annotations

import ctypes
import gc
import logging
import sys
import time

_log = logging.getLogger(__name__)

_MIN_INTERVAL_SECONDS = 60.0  # 两次实际回收之间最短间隔

_last_reclaim: float = 0.0


def release_unused_memory(*, force: bool = False) -> tuple[int, bool]:
    """gc.collect() + (Linux) malloc_trim(0)。

    Args:
        force: 跳过 _MIN_INTERVAL_SECONDS 节流（用于测试/关键节点）

    Returns:
        (collected_objects, trimmed) — collected 为 gc 回收对象数，
        trimmed 为 glibc 是否执行了堆修剪（非 Linux 恒 False）

    永不抛异常：内存回收失败绝不能打断调用方。
    """
    global _last_reclaim
    try:
        now = time.monotonic()
        if not force and (now - _last_reclaim) < _MIN_INTERVAL_SECONDS:
            return (0, False)

        collected = gc.collect()
        trimmed = False
        if sys.platform.startswith("linux"):
            try:
                libc = ctypes.CDLL(None)
                malloc_trim = getattr(libc, "malloc_trim", None)
                if malloc_trim is not None:
                    malloc_trim.argtypes = [ctypes.c_size_t]
                    malloc_trim.restype = ctypes.c_int
                    trimmed = bool(malloc_trim(0))
            except Exception:  # noqa: BLE001
                _log.debug("malloc_trim unavailable", exc_info=True)

        _last_reclaim = now
        return (collected, trimmed)
    except Exception:  # noqa: BLE001
        _log.debug("memory reclaim failed", exc_info=True)
        return (0, False)
