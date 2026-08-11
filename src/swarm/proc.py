"""安全子进程工具 — 行级流式输出 + 取消清理，不留孤儿进程。

移植自 DeepTutor services/subagent/process.py 的设计：
- 逐行 yield stdout/stderr（到达即出，不等到结束）
- 消费方提前退出或取消时，finally 终止子进程（TERMINATE_GRACE_SECONDS 宽限后 kill）
- 无超时等待：子进程自己的逻辑决定何时结束（run_capture 是例外，显式 timeout）

被 scripts/agent_worker.py 与 scripts/swarm_runner.py 的 executor 机制使用：
蜂群自建 agent 通过 --executor-command 把任务 JSON 喂给外部 executor 进程，
run_capture 提供超时与清理保护（替代裸 subprocess.run）。
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Optional, Sequence

TERMINATE_GRACE_SECONDS = 5.0

# (channel, text)，channel ∈ {"stdout", "stderr", "exit"}（exit 的 text 为返回码字符串）
ProcessLine = tuple[str, str]


async def stream_process(
    cmd: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    input: Optional[str] = None,
) -> AsyncIterator[ProcessLine]:
    """spawn 子进程并逐行流式产出。

    Args:
        cmd: 命令及参数（如 ["python3", "-c", "print('hi')"]）
        cwd: 工作目录
        env: 环境变量（默认继承 os.environ，可传覆盖）
        input: 写入 stdin 的初始内容（写入后关闭 stdin；None 则不接 stdin）

    Yields:
        ("stdout" | "stderr", line)；子进程退出后 yield ("exit", "0")

    消费方提前 break/异常/cancel 时：terminate() → 等 TERMINATE_GRACE_SECONDS
    → kill()，不留孤儿进程。
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env or os.environ.copy(),
        stdin=asyncio.subprocess.PIPE if input is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if input is not None and proc.stdin is not None:
        proc.stdin.write(input.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

    queue: asyncio.Queue[ProcessLine] = asyncio.Queue()
    readers: list[asyncio.Task] = []

    async def _pump(stream, channel: str) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                await queue.put((channel, line.decode("utf-8", errors="replace").rstrip("\n")))
            await queue.put(("__eof__", channel))  # EOF 信号，主循环统计
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    for stream, channel in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        if stream is not None:
            readers.append(asyncio.create_task(_pump(stream, channel)))

    try:
        eofs = 0
        while eofs < len(readers):
            channel, text = await queue.get()
            if channel == "__eof__":
                eofs += 1
                continue
            yield (channel, text)

        # 双流 EOF 后收尾：等待进程退出
        exit_code = await proc.wait()
        yield ("exit", str(exit_code))
    finally:
        # 消费方提前退出/取消：终止子进程，不留孤儿
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), TERMINATE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


async def _capture_impl(
    cmd: Sequence[str],
    *,
    cwd: Optional[str],
    env: Optional[dict],
    timeout: float,
    input: Optional[str],
) -> dict:
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    exit_code = -1
    async for channel, text in stream_process(cmd, cwd=cwd, env=env, input=input):
        if channel == "stdout":
            stdout_lines.append(text)
        elif channel == "stderr":
            stderr_lines.append(text)
        else:  # "exit"
            exit_code = int(text)
    return {
        "exit_code": exit_code,
        "stdout": "\n".join(stdout_lines),
        "stderr": "\n".join(stderr_lines),
    }


def run_capture(
    cmd: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    timeout: float = 30.0,
    input: Optional[str] = None,
) -> dict:
    """同步收集 stdout/stderr + exit_code。

    Args:
        cmd: 命令及参数
        cwd: 工作目录
        env: 环境变量
        timeout: 总超时（秒），超时抛 asyncio.TimeoutError；stream_process 的
                 finally 会 terminate/kill 子进程，不留孤儿
        input: 写入 stdin 的初始内容（如 executor JSON payload）

    Returns:
        {"exit_code": int, "stdout": str, "stderr": str}
    """

    async def _run() -> dict:
        return await asyncio.wait_for(
            _capture_impl(cmd, cwd=cwd, env=env, timeout=timeout, input=input),
            timeout,
        )

    return asyncio.run(_run())
