"""
第 1 层修正测试: 角色→技能包映射 (migration 008, 2026-08-11)

覆盖: migration 生效(列存在 + 预置数据) / get_model_profile 解析 load_skills /
build_task_context 注入 Role Skills 段 / 无技能时零注入(向后兼容)。

运行: .venv/bin/python -m pytest tests/test_role_skill_packs.py -q
"""
from __future__ import annotations

import json

import pytest

from src.swarm.model_config import get_model_profile
from src.swarm.worker import build_task_context


def test_migration008_columns_and_seed(db):
    """migration 008 加列 + 5 角色预置技能包"""
    cols = {r["name"] for r in db.fetch_all("PRAGMA table_info(model_profiles)")}
    assert "load_skills" in cols
    assert "tool_allowlist" in cols

    for role in ("scanner", "analyst", "exploiter", "reporter", "custom"):
        row = db.fetch_one(
            "SELECT load_skills, tool_allowlist FROM model_profiles WHERE role=? AND is_default=1",
            (role,),
        )
        assert row is not None, f"role {role} 缺默认 profile"
        skills = json.loads(row["load_skills"] or "[]")
        assert isinstance(skills, list) and skills, f"role {role} 技能包为空"


def test_get_model_profile_parses_skills(db):
    """get_model_profile 返回 load_skills/tool_allowlist (JSON -> list)"""
    profile = get_model_profile(db, "exploiter")
    assert profile is not None
    assert isinstance(profile["load_skills"], list) and profile["load_skills"]
    assert isinstance(profile["tool_allowlist"], list)
    assert any("PoC" in s or "字节" in s or "诊断" in s for s in profile["load_skills"])


def test_build_task_context_injects_role_skills(db, run_id):
    """worker claim 的任务上下文包含 ## Role Skills 段"""
    profile = get_model_profile(db, "analyst")
    task = {
        "task_id": "t-skills-1",
        "run_id": run_id,
        "task_type": "analyze",
        "required_role": "analyst",
        "task_intent": "recon",
        "focus_params": "{}",
        "model_profile": profile,  # claim_once 已解析并塞入
    }
    ctx = build_task_context(db, task)
    assert "## Role Skills" in ctx
    assert "静态分析" in ctx
    # 技能条目逐条注入
    for s in profile["load_skills"]:
        assert s in ctx


def test_build_task_context_without_skills_noop(db, run_id):
    """无 model_profile / 空技能包 -> 上下文不含 Role Skills (向后兼容)"""
    task = {
        "task_id": "t-noskill-1",
        "run_id": run_id,
        "task_type": "analyze",
        "required_role": "custom",
        "task_intent": "recon",
        "focus_params": "{}",
    }
    ctx = build_task_context(db, task)
    assert "## Role Skills" not in ctx

    task["model_profile"] = {"load_skills": []}
    ctx2 = build_task_context(db, task)
    assert "## Role Skills" not in ctx2
