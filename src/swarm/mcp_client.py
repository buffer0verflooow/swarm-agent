"""
Swarm-owned MCP client (Hermes-independent).

A minimal Model Context Protocol client over stdio (JSON-RPC 2.0, one JSON
object per line), implemented with stdlib only (asyncio + subprocess). The
swarm does NOT depend on Hermes' ``config.yaml mcp_servers``; server
definitions live in ``<repo>/mcp_servers.json`` (override with
``SWARM_MCP_CONFIG``):

    {
      "servers": {
        "example": {
          "command": "python3",
          "args": ["/path/to/server.py"],
          "env": {"FOO": "bar"},
          "timeout": 30,
          "cwd": "/path",
          "allow": ["add", "echo"],
          "tools": {"add": {"description": "整数加法 (a+b)"}}
        }
      }
    }

Security model:

- Server commands are fixed at CONFIG time; workers can never supply a
  command — ``call_tool`` only accepts (server, tool, arguments).
- Per-server ``allow`` list restricts callable tools; ``deny`` is also
  honoured. When both are absent every tool the server declares is callable
  (the server itself is operator-configured).
- Every spawn is one-shot: initialize -> call -> terminate. No persistent
  servers, no zombies. ``timeout`` bounds the whole exchange.

Lifecycle: workers invoke ``scripts/mcp_tool.py`` from the executor shell,
so each invocation spawns a fresh client (cheap for stdio servers). The
static ``registry_tool_prompt()`` helper feeds tool availability into the
worker prompt without spawning anything.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent.parent

MCP_PROTOCOL_VERSION = "2024-11-05"

DEFAULT_CONFIG_PATH = REPO / "mcp_servers.json"
DEFAULT_TIMEOUT = 30.0


class MCPError(Exception):
    """Base error for MCP client failures."""


class MCPTimeout(MCPError):
    """Server did not answer within the configured timeout."""


class MCPToolDenied(MCPError):
    """Tool is not in the server's allow list."""


def default_config_path() -> Path:
    env = os.environ.get("SWARM_MCP_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_CONFIG_PATH


def load_mcp_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the MCP server registry; empty registry when file is absent."""
    path = Path(config_path) if config_path else default_config_path()
    if not path.is_file():
        return {"servers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPError(f"invalid MCP config {path}: {exc}") from exc
    servers = data.get("servers") or {}
    if not isinstance(servers, dict):
        raise MCPError(f"MCP config {path}: 'servers' must be an object")
    return {"config_path": str(path), "servers": servers}


def _server_entry(servers: Dict[str, Any], server: str) -> Dict[str, Any]:
    entry = servers.get(server)
    if not isinstance(entry, dict):
        raise MCPError(f"unknown MCP server: {server} (configured: {sorted(servers)})")
    return entry


class MCPClient:
    """One-shot stdio MCP client for a single server.

    Usage (async)::

        async with MCPClient("example", "python3", ["server.py"]) as client:
            tools = await client.list_tools()
            result = await client.call_tool("add", {"a": 1, "b": 2})

    ``start()`` is also callable directly; ``close()`` always terminates the
    subprocess (even on error paths).
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: Optional[str] = None,
    ):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = env or {}
        self.timeout = float(timeout)
        self.cwd = cwd
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._id_counter = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "MCPClient":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        env = dict(os.environ)
        env.update(self.env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.cwd,
            )
        except OSError as exc:
            raise MCPError(f"failed to spawn MCP server '{self.name}' ({self.command}): {exc}") from exc
        assert self._proc.stdin and self._proc.stdout  # created with pipes
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await self._request("initialize", {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "swarm-mcp-client", "version": "1.0"},
            })
            await self._notify("notifications/initialized", {})
        except Exception:
            # 握手失败 (超时/服务器拒绝): __aexit__ 不会执行 (__aenter__ 抛错),
            # 必须在这里自行回收子进程, 避免僵尸。
            await self.close()
            raise

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # ignore non-JSON noise on stdout
                msg_id = msg.get("id")
                if msg_id is None:
                    continue  # server -> client notification; not handled
                fut = self._pending.pop(int(msg_id), None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        err = msg["error"] or {}
                        fut.set_exception(
                            MCPError(f"MCP server '{self.name}' error: {err.get('message') or err}")
                        )
                    else:
                        fut.set_result(msg.get("result") or {})
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MCPError(f"MCP server '{self.name}' closed the stream"))
            self._pending.clear()

    async def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        self._proc.stdin.write((payload + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: Dict[str, Any]) -> Any:
        assert self._proc and self._proc.stdin
        req_id = await self._next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        self._proc.stdin.write((payload + "\n").encode("utf-8"))
        try:
            await self._proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise MCPTimeout(
                f"MCP server '{self.name}' timed out after {self.timeout}s on {method}"
            ) from exc

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._request("tools/list", {})
        tools = result.get("tools") or []
        return [t for t in tools if isinstance(t, dict)]

    async def call_tool(self, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = await self._request(
            "tools/call",
            {"name": tool, "arguments": arguments or {}},
        )
        return result

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            if proc.stdin:
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


class MCPRegistry:
    """Config-backed registry with allowlist enforcement and one-shot calls."""

    def __init__(self, config_path: Optional[Path] = None, timeout: float = DEFAULT_TIMEOUT):
        cfg = load_mcp_config(config_path)
        self.config_path = cfg["config_path"]
        self.servers: Dict[str, Any] = cfg["servers"]
        self.timeout = float(timeout)

    def server_names(self) -> List[str]:
        return sorted(self.servers)

    def _allow_list(self, server: str) -> Optional[List[str]]:
        entry = _server_entry(self.servers, server)
        allow = entry.get("allow")
        if isinstance(allow, list):
            return [str(t) for t in allow]
        return None

    def _deny_list(self, server: str) -> List[str]:
        entry = _server_entry(self.servers, server)
        deny = entry.get("deny")
        if isinstance(deny, list):
            return [str(t) for t in deny]
        return []

    def check_allowed(self, server: str, tool: str) -> None:
        """Raise MCPToolDenied unless the tool is permitted."""
        allow = self._allow_list(server)
        if allow is not None and tool not in allow:
            raise MCPToolDenied(
                f"tool '{tool}' not allowed on MCP server '{server}' (allow={allow})"
            )
        if tool in self._deny_list(server):
            raise MCPToolDenied(f"tool '{tool}' denied on MCP server '{server}'")

    def static_tool_descriptions(self, server: str) -> List[str]:
        """Tool descriptions declared statically in config (no server spawn)."""
        entry = _server_entry(self.servers, server)
        tools = entry.get("tools")
        if not isinstance(tools, dict):
            return []
        out = []
        for name, spec in tools.items():
            desc = spec.get("description", "") if isinstance(spec, dict) else ""
            out.append(f"- server={server} tool={name}: {desc}" if desc else f"- server={server} tool={name}")
        return out

    async def list_tools_live(self, server: str) -> Dict[str, Any]:
        """Spawn the server and fetch its declared tools (for CLI --live)."""
        entry = _server_entry(self.servers, server)
        async with self._client_for(server, entry) as client:
            tools = await client.list_tools()
        return {
            "server": server,
            "tools": tools,
            "allowed": self._allow_list(server) or "*",
            "denied": self._deny_list(server),
        }

    async def call_tool(self, server: str, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Call one tool: allowlist check -> one-shot spawn -> call -> close."""
        self.check_allowed(server, tool)
        entry = _server_entry(self.servers, server)
        async with self._client_for(server, entry) as client:
            result = await client.call_tool(tool, arguments or {})
        content = result.get("content") or []
        is_error = bool(result.get("isError", False))
        text = "\n".join(
            str(c.get("text", "")) for c in content if isinstance(c, dict)
        )
        return {
            "server": server,
            "tool": tool,
            "success": not is_error,
            "is_error": is_error,
            "content": content,
            "text": text,
            "raw": result,
        }

    def _client_for(self, server: str, entry: Dict[str, Any]) -> MCPClient:
        command = entry.get("command")
        if not command or not isinstance(command, str):
            raise MCPError(f"MCP server '{server}' missing 'command' in config")
        return MCPClient(
            name=server,
            command=command,
            args=entry.get("args") or [],
            env=entry.get("env") or {},
            timeout=float(entry.get("timeout") or self.timeout),
            cwd=entry.get("cwd"),
        )


def registry_tool_prompt(server_names: List[str], config_path: Optional[Path] = None) -> str:
    """Build the '## MCP Tools' context section from STATIC config only.

    Never spawns a server. Lists each server's statically declared tools and
    tells the worker how to invoke them through scripts/mcp_tool.py. When a
    server has no static tool descriptions the worker is told to run
    ``mcp_tool.py list`` once to discover them.

    Returns "" when no servers are configured (no context pollution).
    """
    names = [s for s in server_names if isinstance(s, str) and s.strip()]
    if not names:
        return ""
    try:
        registry = MCPRegistry(config_path=config_path)
    except MCPError:
        return ""
    lines = ["## MCP Tools", "通过 scripts/mcp_tool.py 调用 (不依赖 Hermes 的 mcp_servers 配置):"]
    for name in names:
        if name not in registry.servers:
            lines.append(f"- server={name}: 未配置 (mcp_servers.json 缺少该服务器)")
            continue
        descs = registry.static_tool_descriptions(name)
        if descs:
            lines.extend(descs)
        else:
            lines.append(f"- server={name}: 工具清单未在配置中静态声明, 先运行 mcp_tool.py list --server {name} 发现")
        allow = registry._allow_list(name)
        if allow is not None:
            lines.append(f"  白名单: {allow}")
    lines.append(
        "调用格式: python3 scripts/mcp_tool.py call <server> <tool> --args '{\"k\": \"v\"}' "
        "(工具输出即证据, 原样记录)"
    )
    return "\n".join(lines)
