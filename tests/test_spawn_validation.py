"""派发前参数预检测试（DeepTutor tool_arg_guard 模式移植）。

覆盖:
- validate_spawn_request 必填字段/类型/枚举校验
- BaseSpawnHandler.__call__ 对非法请求返回 None
- HermesSpawnHandler 模板 format 不炸 KeyError（缺字段补齐）
"""

from __future__ import annotations

import asyncio

from src.swarm.spawn_handler import (
    KNOWN_ROLES,
    HermesSpawnHandler,
    MockSpawnHandler,
    validate_spawn_request,
)


def _valid_request(**overrides):
    req = {
        "run_id": "run-001",
        "requested_role": "scanner",
        "reason": "scan target",
        "chain_depth": 0,
        "max_chain_depth": 3,
    }
    req.update(overrides)
    return req


def test_valid_request_ok():
    result = validate_spawn_request(_valid_request())
    assert result["ok"] is True
    assert result["errors"] == []


def test_missing_run_id():
    result = validate_spawn_request(_valid_request(run_id=None))
    assert result["ok"] is False
    assert any("field=run_id" in e for e in result["errors"])


def test_missing_role():
    req = _valid_request()
    del req["requested_role"]
    result = validate_spawn_request(req)
    assert result["ok"] is False
    assert any("field=requested_role" in e for e in result["errors"])


def test_unknown_role():
    result = validate_spawn_request(_valid_request(requested_role="hacker"))
    assert result["ok"] is False
    assert any("field=requested_role" in e and "unknown role" in e for e in result["errors"])
    assert any("scanner" in e for e in result["errors"])


def test_bad_chain_depth_type():
    result = validate_spawn_request(_valid_request(chain_depth="abc"))
    assert result["ok"] is False
    assert any("field=chain_depth" in e and "expected int" in e for e in result["errors"])


def test_negative_chain_depth():
    result = validate_spawn_request(_valid_request(chain_depth=-1))
    assert result["ok"] is False
    assert any("field=chain_depth" in e and ">= 0" in e for e in result["errors"])


def test_zero_max_chain_depth():
    result = validate_spawn_request(_valid_request(max_chain_depth=0))
    assert result["ok"] is False
    assert any("field=max_chain_depth" in e and ">= 1" in e for e in result["errors"])


def test_bad_worker_mode_type():
    result = validate_spawn_request(_valid_request(worker_mode="yes"))
    assert result["ok"] is False
    assert any("field=worker_mode" in e for e in result["errors"])


def test_optional_fields_accepted():
    result = validate_spawn_request(
        _valid_request(parent_task_id="task-1", worker_mode=True, reason="")
    )
    assert result["ok"] is True


def test_all_known_roles_valid():
    for role in KNOWN_ROLES:
        result = validate_spawn_request(_valid_request(requested_role=role))
        assert result["ok"] is True, f"role {role} should be valid: {result}"


def test_call_rejects_invalid_request(db, run_id):
    handler = MockSpawnHandler(db)
    result = asyncio.run(
        handler({"run_id": None, "requested_role": "scanner", "reason": "x"}, "ctx")
    )
    assert result is None


def test_call_accepts_valid_request(db, run_id):
    handler = MockSpawnHandler(db)
    result = asyncio.run(handler(_valid_request(run_id=run_id), "ctx"))
    assert result is not None


def test_template_format_no_keyerror(db):
    """缺 reason 的请求不能炸 KeyError，goal 非空。"""
    captured = {}

    def fake_delegate(goal, context):
        captured["goal"] = goal
        return "agent-1"

    handler = HermesSpawnHandler(db, delegate_fn=fake_delegate)
    req = _valid_request(reason=None)
    result = asyncio.run(handler.create_agent(req, "ctx"))
    assert result == "agent-1"
    assert captured["goal"], "goal must be non-empty"
    assert "scan" in captured["goal"] or "扫描" in captured["goal"]


def test_unknown_role_returns_none(db):
    """未知角色在 create_agent 前被拦截（模板无对应条目也安全）。"""
    handler = HermesSpawnHandler(db, delegate_fn=lambda goal, context: "agent-1")
    result = asyncio.run(handler.create_agent(_valid_request(requested_role="hacker"), "ctx"))
    assert result is None
