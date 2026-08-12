#!/usr/bin/env python3
"""
Swarm-owned MCP tool CLI (Hermes-independent).

Lists and calls MCP tools through the swarm's own stdio MCP client
(src/swarm/mcp_client.py). Server definitions live in mcp_servers.json
(override: SWARM_MCP_CONFIG). Workers and external executors (Hermes, Codex,
Claude, opencode — anything with a shell) invoke this script; tool output is
the evidence and must be recorded verbatim.

Examples:
  python3 mcp_tool.py list
  python3 mcp_tool.py list --server example --live
  python3 mcp_tool.py call example add --args '{"a": 1, "b": 2}'
  python3 mcp_tool.py health

stdout is JSON with a ``success`` field so executors can parse it directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.swarm.mcp_client import MCPRegistry, load_mcp_config


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def cmd_list(args) -> int:
    try:
        cfg = load_mcp_config(Path(args.config) if args.config else None)
    except Exception as exc:  # noqa: BLE001
        _print_json({"success": False, "error": str(exc)})
        return 1

    servers = cfg["servers"]
    if args.server:
        if args.server not in servers:
            _print_json({"success": False, "error": f"unknown server: {args.server}"})
            return 1
        servers = {args.server: servers[args.server]}

    if args.live:
        async def _live() -> list:
            registry = MCPRegistry(Path(args.config) if args.config else None)
            out = []
            for name in servers:
                try:
                    out.append(await registry.list_tools_live(name))
                except Exception as exc:  # noqa: BLE001
                    out.append({"server": name, "error": str(exc)})
            return out

        try:
            results = asyncio.run(_live())
        except Exception as exc:  # noqa: BLE001
            _print_json({"success": False, "error": str(exc)})
            return 1
        _print_json({"success": True, "servers": results})
        return 0

    out = {}
    for name, entry in servers.items():
        tools = entry.get("tools") or {}
        if isinstance(tools, dict):
            out[name] = {
                "command": entry.get("command"),
                "tools": list(tools.keys()) if tools else "(live discover: mcp_tool.py list --live)",
                "allow": entry.get("allow", "*"),
            }
        else:
            out[name] = {"command": entry.get("command"), "tools": "(static tools not declared)"}
    _print_json({"success": True, "servers": out})
    return 0


def cmd_call(args) -> int:
    async def _call() -> Dict[str, Any]:
        registry = MCPRegistry(Path(args.config) if args.config else None)
        return await registry.call_tool(args.server, args.tool, args.parsed_args)

    try:
        result = asyncio.run(_call())
    except Exception as exc:  # noqa: BLE001
        _print_json({"success": False, "server": args.server, "tool": args.tool, "error": str(exc)})
        return 1
    _print_json(result)
    return 0 if result["success"] else 2


def cmd_health(args) -> int:
    async def _health() -> list:
        registry = MCPRegistry(Path(args.config) if args.config else None)
        out = []
        for name in registry.server_names():
            try:
                info = await registry.list_tools_live(name)
                out.append({"server": name, "ok": True, "tools": len(info["tools"])})
            except Exception as exc:  # noqa: BLE001
                out.append({"server": name, "ok": False, "error": str(exc)})
        return out

    try:
        results = asyncio.run(_health())
    except Exception as exc:  # noqa: BLE001
        _print_json({"success": False, "error": str(exc)})
        return 1
    ok = all(r.get("ok") for r in results)
    _print_json({"success": ok, "servers": results})
    return 0 if ok else 1


def _parse_args_json(value: str) -> Optional[Dict[str, Any]]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--args must be valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise SystemExit("--args must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Swarm-owned MCP tool CLI")
    parser.add_argument("--config", default="", help="Path to mcp_servers.json (default: repo root / $SWARM_MCP_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="List configured MCP servers and tools")
    list_p.add_argument("--config", default="", help="Path to mcp_servers.json (also accepted before the subcommand)")
    list_p.add_argument("--server", default="", help="Limit to one server")
    list_p.add_argument("--live", action="store_true", help="Spawn servers and fetch declared tools")
    list_p.set_defaults(func=cmd_list)

    call_p = sub.add_parser("call", help="Call one MCP tool")
    call_p.add_argument("--config", default="", help="Path to mcp_servers.json (also accepted before the subcommand)")
    call_p.add_argument("server", help="Server name from mcp_servers.json")
    call_p.add_argument("tool", help="Tool name")
    call_p.add_argument("--args", default="{}", help="JSON object of tool arguments")
    call_p.set_defaults(func=cmd_call)

    health_p = sub.add_parser("health", help="Check every configured server is reachable")
    health_p.add_argument("--config", default="", help="Path to mcp_servers.json (also accepted before the subcommand)")
    health_p.set_defaults(func=cmd_health)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    # 子命令与顶层均可接受 --config (argparse 共享 namespace, 子命令优先)
    if args.command == "call":
        args.parsed_args = _parse_args_json(args.args)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
