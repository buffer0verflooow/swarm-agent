"""安全子进程工具测试（DeepTutor subagent/process.py 移植）。

覆盖:
- run_capture 正常 stdout / stderr / exit_code
- 超时抛 TimeoutError 且清理子进程
- stream_process 异步逐行流式
- 消费方提前 break 不抛异常（finally 清理路径）
"""

from __future__ import annotations

import asyncio

import pytest

from src.swarm.proc import run_capture, stream_process


def test_run_capture_stdout():
    result = run_capture(["python3", "-c", "print('hello')"])
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_run_capture_stderr_and_exit_code():
    result = run_capture(
        ["python3", "-c", "import sys; print('err msg', file=sys.stderr); sys.exit(3)"]
    )
    assert result["exit_code"] == 3
    assert "err msg" in result["stderr"]


def test_run_capture_timeout():
    with pytest.raises(TimeoutError):
        run_capture(
            ["python3", "-c", "import time; time.sleep(60)"],
            timeout=1.0,
        )


def test_run_capture_missing_command():
    with pytest.raises(FileNotFoundError):
        run_capture(["/nonexistent/binary-xyz"], timeout=5.0)


async def _collect_stream(cmd):
    stdout_lines = []
    stderr_lines = []
    exit_code = -1
    async for channel, text in stream_process(cmd):
        if channel == "stdout":
            stdout_lines.append(text)
        elif channel == "stderr":
            stderr_lines.append(text)
        else:
            exit_code = int(text)
    return stdout_lines, stderr_lines, exit_code


def test_stream_process_lines():
    stdout_lines, stderr_lines, exit_code = asyncio.run(
        _collect_stream(["python3", "-c", "print('a'); print('b')"])
    )
    assert stdout_lines == ["a", "b"]
    assert stderr_lines == []
    assert exit_code == 0


def test_stream_process_early_break():
    """消费方提前 break → finally 清理子进程，不抛异常。"""

    async def consume():
        count = 0
        async for _channel, _text in stream_process(
            ["python3", "-c", "import time; [print(i) for i in range(100)]; time.sleep(30)"]
        ):
            count += 1
            if count >= 2:
                break
        return count

    assert asyncio.run(consume()) == 2
