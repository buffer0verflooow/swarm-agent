"""
蜂群技能系统测试 (2026-08-12, migration 009): skills.py 注册表与注入。

覆盖: frontmatter 解析 / discover / load / resolve (名称/标签/旧条目透传) /
注入预算截断 / SWARM_SKILL_PACKS=0 禁用 / import / build_task_context 内容级注入。

运行: .venv/bin/python -m pytest tests/test_swarm_skills.py -q
"""
from __future__ import annotations

import json
import os

import pytest

from src.swarm import skills as skill_mod
from src.swarm.model_config import get_model_profile
from src.swarm.skills import (
    discover_skills,
    import_skill,
    inject_skills_context,
    load_skill,
    parse_frontmatter,
    render_skill_block,
    resolve_skill_ref,
)
from src.swarm.worker import build_task_context

SEED = ["scanner", "analyst", "exploiter", "reporter", "researcher", "custom"]


def test_parse_frontmatter_basic():
    meta = parse_frontmatter("---\nname: analyst\ndescription: 静态分析\ntags: [a, b]\n---\nbody")
    assert meta["name"] == "analyst"
    assert meta["description"] == "静态分析"
    assert meta["tags"] == ["a", "b"]


def test_parse_frontmatter_absent():
    assert parse_frontmatter("no frontmatter here") == {}


def test_discover_seed_skills():
    names = {s["name"] for s in discover_skills()}
    assert set(SEED) <= names


def test_load_skill_missing_returns_none():
    assert load_skill("definitely-not-a-skill") is None


def test_load_skill_returns_body():
    skill = load_skill("analyst")
    assert skill is not None
    assert "静态分析" in skill["body"]
    assert skill["tags"]


def test_resolve_by_name_and_tag():
    assert resolve_skill_ref("analyst") is not None
    # 大小写不敏感
    assert resolve_skill_ref("Analyst") is not None
    # 标签匹配 (exploiter 有 exploit-dev 标签)
    assert resolve_skill_ref("exploit-dev") is not None
    # 无匹配 -> None (调用方按旧条目透传)
    assert resolve_skill_ref("不存在的方法论句子") is None


def test_render_skill_block_truncates():
    skill = {"name": "x", "description": "d", "body": "内容" * 500}
    block = render_skill_block(skill, 400)
    assert "## Skill: x" in block
    assert "截断" in block
    assert len(block) < 800


def test_inject_skills_context_content_level():
    parts: list = []
    injected = inject_skills_context(parts, ["analyst", "exploiter"])
    assert injected == 2
    ctx = "\n".join(parts)
    assert "## Role Skills" in ctx
    assert "## Skill: analyst" in ctx
    assert "静态分析" in ctx  # 内容级注入, 不再只是名字
    assert "## Skill: exploiter" in ctx


def test_inject_skills_context_legacy_passthrough():
    parts: list = []
    injected = inject_skills_context(parts, ["scanner", "旧式方法论句子"])
    assert injected == 1
    ctx = "\n".join(parts)
    assert "## Skill: scanner" in ctx
    assert "## Role Skill Directives" in ctx
    assert "旧式方法论句子" in ctx  # 解析不到的条目按原样透传


def test_inject_skills_context_disabled():
    parts: list = []
    assert inject_skills_context(parts, ["analyst"], enabled=False) == 0
    assert parts == []


def test_inject_skills_context_budget_shared(monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\ndescription: d\n---\n" + "A" * 3000, encoding="utf-8")
    (tmp_path / "b.md").write_text("---\nname: b\ndescription: d\n---\n" + "B" * 3000, encoding="utf-8")
    parts: list = []
    inject_skills_context(parts, ["a", "b"], skills_dir=tmp_path, budget_chars=1000)
    ctx = "\n".join(parts)
    assert "截断" in ctx  # 预算被平分, 单个块超限截断


def test_inject_skills_context_ablation_env(monkeypatch):
    monkeypatch.setenv("SWARM_SKILL_PACKS", "0")
    parts: list = []
    inject_skills_context(parts, ["analyst"])
    assert parts == []


def test_import_skill(tmp_path):
    src = tmp_path / "src.md"
    src.write_text("---\ntags: [x]\n---\nbody text", encoding="utf-8")
    out_dir = tmp_path / "skills"
    result = import_skill(str(src), name="imported-one", skills_dir=out_dir)
    assert result["name"] == "imported-one"
    assert (out_dir / "imported-one.md").is_file()
    # frontmatter 归一化后 name 与文件名一致
    skill = load_skill("imported-one", skills_dir=out_dir)
    assert skill is not None and skill["body"] == "body text"
    with pytest.raises(FileExistsError):
        import_skill(str(src), name="imported-one", skills_dir=out_dir)


def test_build_task_context_injects_skill_content(db, run_id):
    """claim 后上下文含技能真实内容 (migration 009 语义)"""
    profile = get_model_profile(db, "analyst")
    assert profile is not None
    assert "analyst" in profile["load_skills"]
    task = {
        "task_id": "t-skills-content-1",
        "run_id": run_id,
        "task_type": "analyze",
        "required_role": "analyst",
        "task_intent": "recon",
        "focus_params": "{}",
        "model_profile": profile,
    }
    ctx = build_task_context(db, task)
    assert "## Role Skills" in ctx
    assert "## Skill: analyst" in ctx
    assert "静态分析" in ctx


def test_build_task_context_mcp_section_when_servers_configured(db, run_id):
    """model_profile.mcp_servers 配置后注入 MCP Tools 段 (静态, 不拉起进程)"""
    task = {
        "task_id": "t-mcp-1",
        "run_id": run_id,
        "task_type": "analyze",
        "required_role": "analyst",
        "task_intent": "recon",
        "focus_params": "{}",
        "model_profile": {"load_skills": [], "mcp_servers": ["example"]},
    }
    ctx = build_task_context(db, task)
    assert "## MCP Tools" in ctx
    assert "mcp_tool.py" in ctx


def test_build_task_context_no_mcp_section_when_unconfigured(db, run_id):
    task = {
        "task_id": "t-mcp-none-1",
        "run_id": run_id,
        "task_type": "analyze",
        "required_role": "analyst",
        "task_intent": "recon",
        "focus_params": "{}",
        "model_profile": {"load_skills": [], "mcp_servers": []},
    }
    ctx = build_task_context(db, task)
    assert "## MCP Tools" not in ctx
