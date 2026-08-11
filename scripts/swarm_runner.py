#!/usr/bin/env python3
"""
Run an existing swarm with a local multi-worker pool.

The executor command receives JSON on stdin:
  {"task": {...}, "context": "...", "model_profile": {...}}

It should print plain text or JSON compatible with SwarmWorker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import SwarmDB
from src.swarm.command_executor import make_command_executor
from src.swarm.runner import SwarmRunner, adapt_executor_factory


def _ensure_schema(db: SwarmDB) -> None:
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='swarm_runs'"):
        db.init()
        return
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_agent_events'"):
        db.init()
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_artifacts'"):
        db.init()


def _parse_counts(value: str) -> Dict[str, int]:
    if not value:
        return {}
    if value.strip().startswith("{"):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise SystemExit("--role-counts JSON must be an object")
        return {str(k): int(v) for k, v in parsed.items()}
    counts: Dict[str, int] = {}
    for part in value.split(","):
        if not part.strip():
            continue
        role, _, count = part.partition("=")
        if not role or not count:
            raise SystemExit("--role-counts must look like scanner=3,analyst=2")
        counts[role.strip()] = int(count)
    return counts


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Run a swarm with a local multi-worker pool")
    parser.add_argument("--db", default=str(REPO / "swarm_knowledge.db"), help="SQLite DB path")
    parser.add_argument("--run-id", required=True, help="Existing swarm run_id")
    parser.add_argument("--executor-command", required=True, help="Command used by each worker")
    parser.add_argument("--role-counts", default="", help="Override counts, e.g. scanner=3,analyst=2 or JSON")
    parser.add_argument("--max-rounds", type=int, default=50)
    parser.add_argument("--idle-rounds", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    db = SwarmDB(args.db)
    _ensure_schema(db)
    command_executor = make_command_executor(args.executor_command)
    runner = SwarmRunner(db, role_counts=_parse_counts(args.role_counts) or None)
    result = await runner.run_until_idle(
        args.run_id,
        adapt_executor_factory(lambda role, agent_id: command_executor),
        max_rounds=args.max_rounds,
        idle_round_limit=args.idle_rounds,
    )

    # 收尾：写回 run 状态（此前 runner 退出不更新，成功 run 永远显示 running，
    # 只有 failed 靠其他路径标记——2026-08-11 审计执行时发现）
    _final_status = (
        "completed" if (result.task_counts.get("failed") or 0) == 0 else "failed"
    )
    db.execute(
        "UPDATE swarm_runs SET status = ?, updated_at = datetime('now') WHERE run_id = ?",
        (_final_status, result.run_id),
    )
    db.conn.commit()
    db.close()

    payload = {
        "run_id": result.run_id,
        "workers": result.workers,
        "processed": result.processed,
        "rounds": result.rounds,
        "idle_rounds": result.idle_rounds,
        "task_counts": result.task_counts,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(
            f"RUN:{result.run_id} workers={result.workers} processed={result.processed} "
            f"rounds={result.rounds} tasks={result.task_counts}"
        )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
