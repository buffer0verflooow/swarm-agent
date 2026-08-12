#!/usr/bin/env python3
"""
Example stdio MCP server for the swarm-owned MCP client.

Implements the Model Context Protocol over stdio (JSON-RPC 2.0, one JSON
object per line) with two demonstration tools:

  - add:  整数加法, 参数 {"a": int, "b": int} -> text "a+b"
  - echo: 原样回显, 参数 {"text": str} -> text

This is a reference implementation of the wire protocol the swarm client
(spec: src/swarm/mcp_client.py) speaks — NOT a framework dependency. Any MCP
server that speaks stdio JSON-RPC works.

Run: python3 scripts/example_mcp_server.py
"""

from __future__ import annotations

import json
import sys

TOOLS = [
    {
        "name": "add",
        "description": "整数加法, 返回 a+b",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "加数 a"},
                "b": {"type": "integer", "description": "加数 b"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "echo",
        "description": "原样回显输入文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
]


def handle_call(name: str, arguments: dict):
    if name == "add":
        try:
            a = int(arguments.get("a", 0))
            b = int(arguments.get("b", 0))
        except (TypeError, ValueError):
            return {"content": [{"type": "text", "text": "error: a/b 必须是整数"}], "isError": True}
        return {"content": [{"type": "text", "text": str(a + b)}], "isError": False}
    if name == "echo":
        return {"content": [{"type": "text", "text": str(arguments.get("text", ""))}], "isError": False}
    return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "example-mcp-server", "version": "1.0.0"},
                },
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            response = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = msg.get("params") or {}
            result = handle_call(params.get("name", ""), params.get("arguments") or {})
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        else:
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
