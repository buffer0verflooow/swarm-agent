"""外部 executor 命令封装测试（蜂群自建 agent 执行机制）。

覆盖:
- 正常 executor：JSON 输入喂 stdin，JSON 输出解析
- 纯文本输出解析
- 非零退出 → success=False + stderr
- 超时 → success=False + "timed out"（子进程被清理）
- 空命令 → ValueError
"""

from __future__ import annotations

import json

import asyncio

import pytest

from src.swarm.command_executor import DEFAULT_EXECUTOR_TIMEOUT, make_command_executor

TASK = {"task_id": "t1", "required_role": "scanner", "task_type": "scan"}
CTX = "context"


def run_executor(executor, task, ctx):
    """executor 是 async（runner 在事件循环内 await），测试侧用 asyncio.run 同步取结果。"""
    return asyncio.run(executor(task, ctx))


def test_plain_text_output():
    executor = make_command_executor("python3 -c \"import sys; print('hello from executor')\"")
    result = run_executor(executor, TASK, CTX)
    assert result == "hello from executor"


def test_json_output_parsed():
    executor = make_command_executor(
        "python3 -c \"import json,sys; d=json.load(sys.stdin); "
        "print(json.dumps({'content': 'found ' + d['task']['task_id'], 'tags': ['x']}))\""
    )
    result = run_executor(executor, TASK, CTX)
    assert isinstance(result, dict)
    assert result["content"] == "found t1"
    assert result["tags"] == ["x"]


def test_nonzero_exit_returns_failure():
    executor = make_command_executor(
        "python3 -c \"import sys; print('boom', file=sys.stderr); sys.exit(3)\""
    )
    result = run_executor(executor, TASK, CTX)
    assert result["success"] is False
    assert "boom" in result["error"]
    assert result["capture"] is False


def test_timeout_returns_failure():
    """executor 卡死 → 超时失败，而不是挂死调用方。"""
    executor = make_command_executor(
        "python3 -c \"import time; time.sleep(60)\"",
        timeout=1.0,
    )
    result = run_executor(executor, TASK, CTX)
    assert result["success"] is False
    assert "timed out" in result["error"]
    assert "1.0" in result["error"]


def test_empty_command_raises():
    with pytest.raises(ValueError, match="cannot be empty"):
        make_command_executor("   ")


def test_default_timeout_constant():
    assert isinstance(DEFAULT_EXECUTOR_TIMEOUT, float)
    assert DEFAULT_EXECUTOR_TIMEOUT > 0


def test_empty_stdout_returns_no_capture():
    executor = make_command_executor("python3 -c \"\"")
    result = run_executor(executor, TASK, CTX)
    assert result == {"capture": False, "content": ""}
