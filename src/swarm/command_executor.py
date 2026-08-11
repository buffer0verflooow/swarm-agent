"""外部 executor 命令封装 — 蜂群自建 agent 的执行机制。

蜂群脱离 Hermes delegate_task，通过 --executor-command 把任务 JSON
（task/context/model_profile）喂给外部 executor 进程，executor 输出被解析
回 SwarmWorker 流程。executor 可以是任意 CLI——包括 LLM agent CLI。

本模块替代 scripts/agent_worker.py 与 scripts/swarm_runner.py 中重复的
裸 subprocess.run 实现：基于 src.swarm.proc.run_capture 提供超时保护与
子进程清理（executor 卡死 → 任务 fail，而不是挂死整个 worker/runner）。
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Callable, Dict

from .proc import run_capture

DEFAULT_EXECUTOR_TIMEOUT = 300.0  # executor 单任务默认超时（秒）


def _parse_executor_output(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return {"capture": False, "content": ""}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def make_command_executor(
    command: str,
    *,
    timeout: float = DEFAULT_EXECUTOR_TIMEOUT,
) -> Callable[[Dict[str, Any], str], Any]:
    """构造 SwarmWorker 兼容的 executor。

    Args:
        command: 外部 executor 命令（shlex 分割）
        timeout: 单任务执行超时（秒），超时返回失败而非挂死

    Returns:
        executor(task, context) -> 任务 JSON 喂给命令 stdin，
        解析 stdout（JSON 或纯文本）；非零退出/超时返回 success=False
    """
    argv = shlex.split(command)
    if not argv:
        raise ValueError("--executor-command cannot be empty")

    def executor(task: Dict[str, Any], context: str) -> Any:
        payload = json.dumps(
            {
                "task": task,
                "context": context,
                "model_profile": task.get("model_profile"),
            },
            ensure_ascii=False,
        )
        try:
            result = run_capture(argv, input=payload, timeout=timeout)
        except TimeoutError:
            return {
                "success": False,
                "error": f"executor timed out after {timeout}s",
                "capture": False,
            }
        if result["exit_code"] != 0:
            return {
                "success": False,
                "error": result["stderr"].strip() or f"executor exited {result['exit_code']}",
                "capture": False,
            }
        return _parse_executor_output(result["stdout"])

    return executor
