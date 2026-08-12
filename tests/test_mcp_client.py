"""
蜂群 MCP 客户端测试 (2026-08-12): src/swarm/mcp_client.py + scripts/mcp_tool.py。

用仓库内 scripts/example_mcp_server.py 作为真实 stdio 服务器端到端验证:
initialize 握手 / tools/list / tools/call / allowlist 拒绝 / 超时 / CLI。

运行: .venv/bin/python -m pytest tests/test_mcp_client.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.swarm.mcp_client import (  # noqa: E402
    MCPClient,
    MCPError,
    MCPRegistry,
    MCPTimeout,
    MCPToolDenied,
    load_mcp_config,
    registry_tool_prompt,
)

EXAMPLE_SERVER = str(REPO / "scripts" / "example_mcp_server.py")


def _server_cfg(**overrides):
    cfg = {
        "command": sys.executable,
        "args": [EXAMPLE_SERVER],
        "timeout": 10,
        "allow": ["add", "echo"],
        "tools": {"add": {"description": "整数加法"}, "echo": {"description": "回显"}},
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture()
def registry_config(tmp_path):
    """临时 mcp_servers.json: 允许 add+echo 的 example 服务器 + 仅 echo 的受限服务器"""
    cfg = {
        "servers": {
            "example": _server_cfg(),
            "restricted": _server_cfg(allow=["echo"]),
            "silent": _server_cfg(timeout=1, allow=["never"]),
        }
    }
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return str(path)


def test_load_mcp_config_missing_file(tmp_path):
    cfg = load_mcp_config(tmp_path / "nope.json")
    assert cfg["servers"] == {}


def test_load_mcp_config_invalid(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MCPError):
        load_mcp_config(path)


async def test_client_handshake_list_call():
    async with MCPClient("example", sys.executable, [EXAMPLE_SERVER], timeout=10) as client:
        tools = await client.list_tools()
        names = {t["name"] for t in tools}
        assert {"add", "echo"} <= names
        result = await client.call_tool("add", {"a": 20, "b": 22})
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        assert text == "42"


async def test_client_unknown_tool_returns_error_not_raise():
    async with MCPClient("example", sys.executable, [EXAMPLE_SERVER], timeout=10) as client:
        result = await client.call_tool("nonexistent", {})
    assert result.get("isError") is True


async def test_client_timeout(tmp_path):
    """服务器不应答 -> MCPTimeout (且不泄漏子进程)"""
    import asyncio

    silent = tmp_path / "silent_server.py"
    silent.write_text(
        "import sys\nfor line in sys.stdin:\n    pass\n",
        encoding="utf-8",
    )
    client = MCPClient("silent", sys.executable, [str(silent)], timeout=1.0)
    with pytest.raises(MCPTimeout):
        async with client:
            await asyncio.sleep(0.05)
    # start() 握手失败时自行 close(), 子进程已回收
    assert client._proc is None


async def test_client_missing_command():
    with pytest.raises(MCPError):
        async with MCPClient("ghost", "/nonexistent/bin/ghost-server"):
            pass


async def test_registry_call_tool(registry_config):
    registry = MCPRegistry(Path(registry_config))
    result = await registry.call_tool("example", "add", {"a": 1, "b": 2})
    assert result["success"] is True
    assert result["text"] == "3"


async def test_registry_allowlist_enforced(registry_config):
    registry = MCPRegistry(Path(registry_config))
    # restricted 服务器只允许 echo, 调 add 被拒且不拉起进程
    with pytest.raises(MCPToolDenied):
        await registry.call_tool("restricted", "add", {"a": 1, "b": 2})


def test_registry_check_allowed_deny(registry_config, tmp_path):
    cfg = json.loads(Path(registry_config).read_text(encoding="utf-8"))
    cfg["servers"]["denied"] = _server_cfg(deny=["echo"])
    path = tmp_path / "mcp_deny.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    registry = MCPRegistry(path)
    with pytest.raises(MCPToolDenied):
        registry.check_allowed("denied", "echo")
    registry.check_allowed("denied", "add")  # deny 只挡 echo


def test_registry_unknown_server(registry_config):
    registry = MCPRegistry(Path(registry_config))
    with pytest.raises(MCPError):
        registry.check_allowed("ghost", "add")


def test_registry_tool_prompt_static(registry_config):
    prompt = registry_tool_prompt(["example", "restricted", "ghost"], Path(registry_config))
    assert "## MCP Tools" in prompt
    assert "server=example tool=add" in prompt
    assert "白名单" in prompt
    assert "ghost" in prompt  # 未配置服务器也有提示


def test_registry_tool_prompt_empty():
    assert registry_tool_prompt([]) == ""


async def test_registry_list_tools_live(registry_config):
    registry = MCPRegistry(Path(registry_config))
    info = await registry.list_tools_live("example")
    assert {t["name"] for t in info["tools"]} >= {"add", "echo"}
    assert info["allowed"] == ["add", "echo"]


def test_cli_list_and_call(registry_config):
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mcp_tool.py"), "list", "--config", registry_config],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert out.returncode == 0
    payload = json.loads(out.stdout)
    assert payload["success"] is True
    assert "example" in payload["servers"]

    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mcp_tool.py"), "call", "example", "add",
         "--args", '{"a": 5, "b": 7}', "--config", registry_config],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert out.returncode == 0
    payload = json.loads(out.stdout)
    assert payload["success"] is True
    assert payload["text"] == "12"


def test_cli_denied_tool(registry_config):
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mcp_tool.py"), "call", "restricted", "add",
         "--args", "{}", "--config", registry_config],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert out.returncode == 1
    payload = json.loads(out.stdout)
    assert payload["success"] is False
    assert "not allowed" in payload["error"]


def test_cli_health(registry_config):
    env = dict(os.environ)
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mcp_tool.py"), "health", "--config", registry_config],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert out.returncode == 0
    payload = json.loads(out.stdout)
    assert payload["success"] is True
    assert all(s["ok"] for s in payload["servers"])
