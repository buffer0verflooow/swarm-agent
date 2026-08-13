"""
任务→角色→技能 索引表测试 (migration 010, 2026-08-12).

覆盖: migration 生效(表+种子) / resolve 命中与回退 / publish_work_task 固化
任务级技能与工具 / build_task_context 合并任务级技能与角色技能 /
工具白名单注入 / 未命中 task_type 向后兼容。

运行: .venv/bin/python -m pytest tests/test_task_skill_index.py -q
"""

from __future__ import annotations

import json

import pytest

from src.swarm.task_skills import index_to_focus, resolve_task_skills
from src.swarm.worker import build_task_context
from src.swarm.work_queue import publish_work_task


def test_migration010_table_and_seed(db):
    """索引表存在 + 5 个默认任务类型种子。"""
    rows = db.fetch_all("SELECT task_type, role, load_skills, tool_allowlist FROM task_skill_index")
    by_type = {r["task_type"]: r for r in rows}
    assert len(by_type) >= 5
    assert by_type["scan"]["role"] == "scanner"
    assert by_type["exploit"]["role"] == "exploiter"
    assert json.loads(by_type["analyze"]["load_skills"]) == ["analyst"]
    assert "gdb" in json.loads(by_type["exploit"]["tool_allowlist"])


def test_resolve_hit(db):
    """命中: 返回 role + 技能 + 工具。"""
    r = resolve_task_skills(db, "analyze")
    assert r is not None
    assert r["role"] == "analyst"
    assert r["load_skills"] == ["analyst"]
    assert "readelf" in r["tool_allowlist"]


def test_resolve_research_hit(db):
    """research 产品线 (migration 016): 独立 researcher 角色 + research 技能,
    不含二进制分析工具。"""
    r = resolve_task_skills(db, "research")
    assert r is not None
    assert r["role"] == "researcher"
    assert r["load_skills"] == ["researcher"]
    assert "readelf" not in r["tool_allowlist"]
    assert "objdump" not in r["tool_allowlist"]


def test_resolve_miss_returns_none(db):
    """未命中: 返回 None (调用方回退旧映射, 技能/工具为空)。"""
    assert resolve_task_skills(db, "no-such-task-type") is None
    assert resolve_task_skills(db, "") is None


def test_publish_work_task_carries_index(db, run_id):
    """publish 时: role 按索引表推导, focus 固化 task_skills/task_tools。"""
    task_id = publish_work_task(
        db,
        run_id=run_id,
        task_type="analyze",
        required_role=None,
        reason="静态分析目标二进制",
    )
    row = db.fetch_one(
        "SELECT required_role, focus_params FROM agent_tasks WHERE task_id = ?",
        (task_id,),
    )
    assert row["required_role"] == "analyst"
    focus = json.loads(row["focus_params"])
    assert focus["task_skills"] == ["analyst"]
    assert "readelf" in focus["task_tools"]


def test_publish_work_task_explicit_role_wins(db, run_id):
    """显式 required_role 覆盖索引表推导 (任务方指定优先)。"""
    task_id = publish_work_task(
        db,
        run_id=run_id,
        task_type="analyze",
        required_role="exploiter",
        reason="分析但指定利用者执行",
    )
    row = db.fetch_one("SELECT required_role FROM agent_tasks WHERE task_id = ?", (task_id,))
    assert row["required_role"] == "exploiter"


def test_publish_research_task_carries_researcher_skills(db, run_id):
    """research 任务: role=researcher, focus 固化 research 技能与工具。"""
    task_id = publish_work_task(
        db,
        run_id=run_id,
        task_type="research",
        required_role=None,
        reason="调研竞品技术方案",
        intent="research",
    )
    row = db.fetch_one(
        "SELECT required_role, focus_params FROM agent_tasks WHERE task_id = ?",
        (task_id,),
    )
    assert row["required_role"] == "researcher"
    focus = json.loads(row["focus_params"])
    assert focus["task_skills"] == ["researcher"]
    assert "curl" in focus["task_tools"]


def test_build_task_context_merges_task_skills(db, run_id):
    """build_task_context: 任务级技能 + 角色技能合并注入 (任务级优先)。"""
    task_id = publish_work_task(
        db,
        run_id=run_id,
        task_type="exploit",
        required_role=None,
        reason="利用开发",
    )
    task = db.fetch_one(
        "SELECT * FROM agent_tasks WHERE task_id = ?",
        (task_id,),
    )
    ctx = build_task_context(db, dict(task))
    assert "## Role Skills" in ctx
    # 任务级技能 (索引表 exploit → exploiter.md) 内容注入
    assert "## Skill: exploiter" in ctx
    # 工具白名单段
    assert "## Task Tool Allowlist" in ctx
    assert "- gdb" in ctx


def test_index_to_focus_empty():
    """无索引结果时不产生 focus 子字段。"""
    assert index_to_focus(None) == {}
    assert index_to_focus({"role": "analyst", "load_skills": [], "tool_allowlist": []}) == {}


def test_unknown_task_type_legacy_behavior(db, run_id):
    """索引表未命中的合法 task_type (如 subtask): 回退 custom 角色, 无任务级技能。

    注: agent_tasks.task_type 有 CHECK 约束 (scan/analyze/exploit/report/subtask/
    custom), 索引表扩展新类型需同步放宽该约束 (数据库演进边界)。
    """
    task_id = publish_work_task(
        db,
        run_id=run_id,
        task_type="subtask",
        required_role=None,
        reason="未知类型",
    )
    row = db.fetch_one(
        "SELECT required_role, focus_params FROM agent_tasks WHERE task_id = ?",
        (task_id,),
    )
    assert row["required_role"] == "custom"
    focus = json.loads(row["focus_params"])
    assert "task_skills" not in focus
    assert "task_tools" not in focus
