"""worker 终止 label 测试（DeepTutor LabelProtocol 移植）。

覆盖:
- DONE/BLOCKED/EXHAUSTED 终止 label 的 status 映射
- 未知 label 忽略
- 未知角色用 DEFAULT_TERMINAL_LABELS
- run_loop 收到终止 label 后退出，不再空转
"""

from __future__ import annotations

import asyncio

from src.swarm.worker import SwarmWorker
from src.swarm.work_queue import publish_work_task


def _make_worker(db, run_id, role="scanner", executor=None, agent_id="worker-a"):
    return SwarmWorker(
        db,
        run_id=run_id,
        agent_id=agent_id,
        role=role,
        executor=executor,
        poll_interval=0.05,
    )


def _publish(db, run_id, role="scanner", task_type="scan", intent=""):
    return publish_work_task(
        db,
        run_id=run_id,
        task_type=task_type,
        required_role=role,
        reason="test task",
        intent=intent,
    )


def _run_once(db, run_id, role, executor):
    worker = _make_worker(db, run_id, role=role, executor=executor)
    return asyncio.run(worker.run_once())


def test_done_label(db, run_id):
    task_id = _publish(db, run_id)
    result = _run_once(
        db, run_id, "scanner", lambda task, ctx: {"content": "scanned", "final_label": "DONE"}
    )
    assert result is not None
    assert result.status == "completed"
    assert result.final_label == "DONE"
    row = db.fetch_one("SELECT status FROM agent_tasks WHERE task_id = ?", (task_id,))
    assert row["status"] == "completed"


def test_blocked_label(db, run_id):
    task_id = _publish(db, run_id)
    result = _run_once(
        db, run_id, "scanner",
        lambda task, ctx: {"content": "blocked by CF", "final_label": "BLOCKED"},
    )
    assert result is not None
    assert result.status == "failed"
    assert result.final_label == "BLOCKED"
    assert "BLOCKED" in result.error
    row = db.fetch_one("SELECT status FROM agent_tasks WHERE task_id = ?", (task_id,))
    assert row["status"] == "failed"


def test_exhausted_label(db, run_id):
    task_id = _publish(db, run_id)
    result = _run_once(
        db, run_id, "scanner",
        lambda task, ctx: {"content": "ran out of paths", "final_label": "EXHAUSTED"},
    )
    assert result is not None
    assert result.status == "completed"
    assert result.final_label == "EXHAUSTED"
    row = db.fetch_one("SELECT status FROM agent_tasks WHERE task_id = ?", (task_id,))
    assert row["status"] == "completed"


def test_unknown_label_ignored(db, run_id):
    task_id = _publish(db, run_id)
    result = _run_once(
        db, run_id, "scanner",
        lambda task, ctx: {"content": "x", "final_label": "FROBNICATE"},
    )
    assert result is not None
    assert result.status == "completed"
    assert result.final_label == ""
    row = db.fetch_one("SELECT status FROM agent_tasks WHERE task_id = ?", (task_id,))
    assert row["status"] == "completed"


def test_custom_role_uses_default_labels(db, run_id):
    task_id = _publish(db, run_id, role="custom", task_type="custom")
    result = _run_once(
        db, run_id, "custom",
        lambda task, ctx: {"content": "nope", "final_label": "BLOCKED"},
    )
    assert result is not None
    assert result.status == "failed"
    assert result.final_label == "BLOCKED"
    row = db.fetch_one("SELECT status FROM agent_tasks WHERE task_id = ?", (task_id,))
    assert row["status"] == "failed"


def test_reporter_has_no_exhausted_label(db, run_id):
    """reporter 终止集合不含 EXHAUSTED → 该 label 被忽略。"""
    task_id = _publish(db, run_id, role="reporter", task_type="report")
    result = _run_once(
        db, run_id, "reporter",
        lambda task, ctx: {"content": "report", "final_label": "EXHAUSTED"},
    )
    assert result is not None
    assert result.status == "completed"
    assert result.final_label == ""


def test_run_loop_exits_on_terminal_label(db, run_id):
    """两个任务：第一个正常，第二个 DONE → 循环在第二个任务后退出。

    注意：publish_work_task 按 signal_key 去重，两个任务须用不同 intent
    才能生成不同 signal_key，否则第二个 publish 返回同一个任务。
    """
    _publish(db, run_id, intent="phase-1")
    _publish(db, run_id, intent="phase-2")

    results = [
        {"content": "first scan"},
        {"content": "second scan", "final_label": "DONE"},
    ]
    state = {"i": 0}

    def executor(task, ctx):
        item = results[state["i"]]
        state["i"] += 1
        return item

    worker = _make_worker(db, run_id, role="scanner", executor=executor)
    stats = asyncio.run(worker.run_loop())
    assert stats["processed"] == 2
    assert stats["final_label"] == "DONE"
