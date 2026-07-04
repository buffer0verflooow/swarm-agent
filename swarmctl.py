#!/usr/bin/env python3
"""
Swarm control CLI.

External clients submit top-level tasks to the swarm and fetch results through
this script. Model profile commands are administrative controls for the swarm
runtime, not per-client worker assignment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src import SwarmDB
from src.swarm.client_api import (
    get_swarm_result,
    get_swarm_status,
    submit_swarm_task,
    wait_for_swarm_result,
)
from src.swarm.model_config import (
    build_run_summary,
    get_model_profile,
    list_model_profiles,
    record_swarm_event,
    upsert_model_profile,
)

ROLES = ["scanner", "analyst", "exploiter", "reporter", "orchestrator", "custom"]
INTENTS = ["recon", "exploit", "analyze", "defend", "report", "custom"]
TARGET_TYPES = ["ip", "binary", "apk", "webapp", "domain", "network", "unknown"]


def _ensure_schema(db: SwarmDB) -> None:
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='swarm_runs'"):
        db.init()
        return
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='model_profiles'"):
        db.init()


def _json_arg(value: str, name: str) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"{name} must be a JSON object")
    return parsed


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_models_list(db: SwarmDB, args) -> int:
    profiles = list_model_profiles(db, role=args.role, enabled_only=args.enabled_only)
    if args.json:
        _print_json({"profiles": profiles})
        return 0

    if not profiles:
        print("No model profiles configured")
        return 0

    for profile in profiles:
        default = " default" if profile["is_default"] else ""
        enabled = "enabled" if profile["enabled"] else "disabled"
        print(
            f"{profile['profile_id']} role={profile['role']} "
            f"model={profile['provider']}/{profile['model']} "
            f"priority={profile['priority']} {enabled}{default}"
        )
    return 0


def cmd_models_set(db: SwarmDB, args) -> int:
    profile_id = upsert_model_profile(
        db,
        role=args.role,
        provider=args.provider,
        model=args.model,
        profile_id=args.profile_id or None,
        priority=args.priority,
        is_default=args.is_default,
        enabled=not args.disabled,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        tool_policy=_json_arg(args.tool_policy, "--tool-policy"),
        system_prompt=args.system_prompt,
        metadata=_json_arg(args.metadata, "--metadata"),
    )
    profile = get_model_profile(db, args.role, profile_id=profile_id)
    if args.json:
        _print_json({"profile_id": profile_id, "profile": profile})
    else:
        print(f"PROFILE:{profile_id}")
        if profile:
            print(f"{profile['role']} -> {profile['provider']}/{profile['model']}")
    return 0


def cmd_event(db: SwarmDB, args) -> int:
    event_id = record_swarm_event(
        db,
        run_id=args.run_id,
        event_type=args.type,
        source=args.source,
        agent_id=args.agent or None,
        task_id=args.task_id or None,
        content=args.content,
        metadata=_json_arg(args.metadata, "--metadata"),
    )
    payload = {"event_id": event_id, "run_id": args.run_id}
    if args.update_summary:
        payload["summary"] = build_run_summary(db, args.run_id, limit_events=args.limit_events)

    if args.json:
        _print_json(payload)
    else:
        print(f"EVENT:{event_id}")
    return 0


def cmd_summary(db: SwarmDB, args) -> int:
    summary = build_run_summary(db, args.run_id, limit_events=args.limit_events)
    if args.json:
        _print_json(summary)
    else:
        print(summary["summary"])
    return 0


def cmd_task_submit(db: SwarmDB, args) -> int:
    result = submit_swarm_task(
        db,
        task=args.task,
        client_source=args.source,
        intent=args.intent,
        target_type=args.target_type,
        target_id=args.target,
        profile=args.profile,
        swarm_name=args.name,
        metadata=_json_arg(args.metadata, "--metadata"),
    )
    if args.json:
        _print_json(result)
    else:
        print(f"RUN:{result['run_id']}")
        print(f"Request: {result['request_id']}")
        print(f"Seeded tasks: {len(result['seeded_tasks'])}")
    return 0


def cmd_task_status(db: SwarmDB, args) -> int:
    status = get_swarm_status(db, args.run_id)
    if args.json:
        _print_json(status)
    else:
        print(f"RUN:{status['run_id']} status={status['status']} ready={status['ready']}")
        print("Tasks: " + (", ".join(f"{k}={v}" for k, v in sorted(status["tasks"].items())) or "none"))
    return 0


def cmd_task_result(db: SwarmDB, args) -> int:
    result = get_swarm_result(db, args.run_id, limit_events=args.limit_events)
    if args.json:
        _print_json(result)
    else:
        print(result["result"])
    return 0


def cmd_task_wait(db: SwarmDB, args) -> int:
    result = wait_for_swarm_result(
        db,
        args.run_id,
        timeout_seconds=args.timeout,
        poll_interval=args.interval,
        limit_events=args.limit_events,
    )
    if args.json:
        _print_json(result)
    else:
        print(result["result"])
        if result.get("timed_out"):
            print(f"\nTimed out waiting for run {args.run_id}", file=sys.stderr)
    return 1 if result.get("timed_out") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control swarm model config and run summaries")
    parser.add_argument("--db", default=str(REPO / "swarm_knowledge.db"), help="SQLite DB path")
    sub = parser.add_subparsers(dest="command", required=True)

    task = sub.add_parser("task", help="Submit top-level work to the swarm and fetch results")
    task_sub = task.add_subparsers(dest="task_command", required=True)

    task_submit = task_sub.add_parser("submit", help="Submit a task to the swarm")
    task_submit.add_argument("--source", required=True, help="External caller, e.g. claude, hermes, codex")
    task_submit.add_argument("--task", required=True, help="Top-level objective for the swarm")
    task_submit.add_argument("--intent", default="custom", choices=INTENTS)
    task_submit.add_argument("--target-type", default="unknown", choices=TARGET_TYPES)
    task_submit.add_argument("--target", default="", help="Optional target identifier")
    task_submit.add_argument("--profile", default="balanced", choices=["balanced", "breadth", "depth"])
    task_submit.add_argument("--name", default="", help="Optional swarm run name")
    task_submit.add_argument("--metadata", default="{}", help="JSON object")
    task_submit.add_argument("--json", action="store_true")
    task_submit.set_defaults(func=cmd_task_submit)

    task_status = task_sub.add_parser("status", help="Fetch swarm run status")
    task_status.add_argument("--run-id", required=True)
    task_status.add_argument("--json", action="store_true")
    task_status.set_defaults(func=cmd_task_status)

    task_result = task_sub.add_parser("result", help="Fetch swarm run result")
    task_result.add_argument("--run-id", required=True)
    task_result.add_argument("--limit-events", type=int, default=10)
    task_result.add_argument("--json", action="store_true")
    task_result.set_defaults(func=cmd_task_result)

    task_wait = task_sub.add_parser("wait", help="Wait for a swarm run to finish and print result")
    task_wait.add_argument("--run-id", required=True)
    task_wait.add_argument("--timeout", type=float, default=300.0)
    task_wait.add_argument("--interval", type=float, default=5.0)
    task_wait.add_argument("--limit-events", type=int, default=10)
    task_wait.add_argument("--json", action="store_true")
    task_wait.set_defaults(func=cmd_task_wait)

    models = sub.add_parser("models", help="Manage swarm-owned model profiles")
    models_sub = models.add_subparsers(dest="models_command", required=True)

    models_list = models_sub.add_parser("list", help="List model profiles")
    models_list.add_argument("--role", choices=ROLES)
    models_list.add_argument("--enabled-only", action="store_true")
    models_list.add_argument("--json", action="store_true")
    models_list.set_defaults(func=cmd_models_list)

    models_set = models_sub.add_parser("set", help="Create or update a model profile")
    models_set.add_argument("--role", required=True, choices=ROLES)
    models_set.add_argument("--provider", required=True, help="Provider name, e.g. client, claude, codex")
    models_set.add_argument("--model", required=True, help="Model name or client policy class")
    models_set.add_argument("--profile-id", default="")
    models_set.add_argument("--priority", type=int, default=50)
    models_set.add_argument("--default", dest="is_default", action="store_true")
    models_set.add_argument("--disabled", action="store_true")
    models_set.add_argument("--max-tokens", type=int)
    models_set.add_argument("--temperature", type=float)
    models_set.add_argument("--tool-policy", default="{}", help="JSON object")
    models_set.add_argument("--system-prompt", default="")
    models_set.add_argument("--metadata", default="{}", help="JSON object")
    models_set.add_argument("--json", action="store_true")
    models_set.set_defaults(func=cmd_models_set)

    event = sub.add_parser("event", help="Record an external client conversation event")
    event.add_argument("--run-id", required=True)
    event.add_argument("--type", required=True, help="Event type, e.g. client_message, task_completed")
    event.add_argument("--source", required=True, help="External caller, e.g. claude, hermes, codex")
    event.add_argument("--content", required=True)
    event.add_argument("--agent", default="")
    event.add_argument("--task-id", default="")
    event.add_argument("--metadata", default="{}", help="JSON object")
    event.add_argument("--update-summary", action="store_true")
    event.add_argument("--limit-events", type=int, default=10)
    event.add_argument("--json", action="store_true")
    event.set_defaults(func=cmd_event)

    summary = sub.add_parser("summary", help="Build and print a run summary")
    summary.add_argument("--run-id", required=True)
    summary.add_argument("--limit-events", type=int, default=10)
    summary.add_argument("--json", action="store_true")
    summary.set_defaults(func=cmd_summary)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    db = SwarmDB(args.db)
    try:
        _ensure_schema(db)
        raise SystemExit(args.func(db, args))
    finally:
        db.close()


if __name__ == "__main__":
    main()
