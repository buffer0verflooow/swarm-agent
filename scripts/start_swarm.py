#!/usr/bin/env python3
"""
Start a swarm run by seeding the shared work market.

This replaces fixed four-stage initialization. The run starts with independent
market tasks; workers claim by role and discoveries create follow-up work.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src import SwarmDB
from src.swarm.run_manager import create_seeded_swarm_run


def _ensure_schema(db: SwarmDB) -> None:
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='swarm_runs'"):
        db.init()
        return
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='model_profiles'"):
        db.init()
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_agent_events'"):
        db.init()
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_artifacts'"):
        db.init()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a market-driven swarm run")
    parser.add_argument("--db", default=str(REPO / "swarm_knowledge.db"), help="SQLite DB path")
    parser.add_argument("--name", default="swarm-run", help="Swarm run name")
    parser.add_argument("--intent", default="recon", choices=["recon", "exploit", "analyze", "defend", "report", "custom"])
    parser.add_argument("--target-type", default="webapp", choices=["ip", "binary", "apk", "webapp", "domain", "network", "unknown"])
    parser.add_argument("--target", required=True, help="Target identifier")
    parser.add_argument("--profile", default="balanced", choices=["balanced", "breadth", "depth"])
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    db = SwarmDB(args.db)
    _ensure_schema(db)

    result = create_seeded_swarm_run(
        db,
        swarm_name=args.name,
        intent=args.intent,
        target_type=args.target_type,
        target_id=args.target,
        profile=args.profile,
    )
    db.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, default=str))
        return

    run_id = result["run_id"]
    print(f"RUN:{run_id}")
    print(f"Seeded tasks: {len(result['seeded_tasks'])}")
    print(f"Min agents: {json.dumps(result.get('min_agents_by_role', {}), ensure_ascii=False)}")
    for task in result["seeded_tasks"]:
        profile = task.get("model_profile") or {}
        model = (
            f"{profile.get('provider')}/{profile.get('model')}"
            if profile else "unassigned"
        )
        print(
            f"- {task['required_role']}:{task['task_type']} p={task['priority']} "
            f"{task['task_id'][:8]} model={model} {task['name']}"
        )

    role_counts = result.get("min_agents_by_role") or {
        role: 1 for role in sorted({task["required_role"] for task in result["seeded_tasks"]})
    }
    print("\nWorker claim commands:")
    for role, count in sorted(role_counts.items()):
        for idx in range(1, int(count) + 1):
            print(
                "python3 "
                f"{REPO / 'agent_worker.py'} --db {args.db} --run-id {run_id} "
                f"--agent {role}-{idx:02d} --role {role} --claim-only"
            )


if __name__ == "__main__":
    main()
