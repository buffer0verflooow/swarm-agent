"""swarmctl run/query 子命令集成测试（吸收 start_swarm.py / query_kb.py 后）。

覆盖:
- build_parser 注册 run/query 子命令
- cmd_run: 创建 seeded run，返回 RUN id 与 seeded 任务
- cmd_query: 知识库查询（空库不炸，返回空）
- cmd_run --json 输出结构
"""

from __future__ import annotations

import argparse
import json

import pytest

from scripts.swarmctl import build_parser, cmd_query, cmd_run

REQUIRED = ("--target", "test-target.example.com")


def _parse(argv):
    return build_parser().parse_args(argv)


def test_parser_has_run_and_query():
    # 行为验证：run/query 子命令可解析（不依赖 argparse 私有结构）
    run_args = _parse(["run", *REQUIRED])
    assert run_args.command == "run"
    query_args = _parse(["query", "--query", "auth"])
    assert query_args.command == "query"
    assert query_args.query == "auth"


def test_parser_run_requires_target():
    with pytest.raises(SystemExit):
        _parse(["run", "--intent", "recon"])


def test_cmd_run_creates_run(db, capsys):
    args = _parse(["run", *REQUIRED])
    rc = cmd_run(db, args)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("RUN:")
    run_id = out.split()[0].split(":", 1)[1]
    run = db.fetch_one("SELECT run_id, intent, target_id FROM swarm_runs WHERE run_id = ?", (run_id,))
    assert run is not None
    assert run["target_id"] == "test-target.example.com"
    assert "Worker claim commands" in out


def test_cmd_run_json(db, capsys):
    args = _parse(["run", *REQUIRED, "--json"])
    rc = cmd_run(db, args)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["run_id"]
    assert payload["seeded_tasks"]


def test_cmd_query_empty_db(db, capsys):
    args = _parse(["query", "--query", "login", "--level-min", "1"])
    rc = cmd_query(db, args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "No knowledge entries" in out


def test_cmd_query_json(db, capsys):
    args = _parse(["query", "--query", "auth", "--json"])
    rc = cmd_query(db, args)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["query"] == "auth"
    assert "results" in payload
