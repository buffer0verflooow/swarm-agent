#!/usr/bin/env python3
"""
Swarm worker CLI.

Examples:
  Claim one task and print task/context for a human or LLM agent:
    python3 agent_worker.py --run-id RUN --agent scanner-1 --role scanner --claim-only

  Run one task through an external executor command:
    python3 agent_worker.py --run-id RUN --agent analyst-1 --role analyst \
      --executor-command ./run_analyst_once

The executor receives JSON on stdin:
  {"task": {...}, "context": "...", "model_profile": {...}}

It may print plain text or JSON:
  {"content": "...", "tags": ["x"], "token_cost": 123}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src import SwarmDB
from src.agents.capture import CaptureContext, CaptureSource, capture
from src.swarm.model_config import record_swarm_event, resolve_task_model_profile
from src.swarm.artifacts import verify_artifacts
from src.swarm.work_queue import complete_work_task, fail_work_task
from src.swarm.worker import SwarmWorker


def _ensure_schema(db: SwarmDB) -> None:
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_tasks'"):
        db.init()
        return
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='model_profiles'"):
        db.init()
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_agent_events'"):
        db.init()
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_artifacts'"):
        db.init()


def _parse_executor_output(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        return {"capture": False, "content": ""}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def make_command_executor(command: str):
    argv = shlex.split(command)
    if not argv:
        raise ValueError("--executor-command cannot be empty")

    def executor(task: Dict[str, Any], context: str) -> Any:
        payload = json.dumps(
            {
                "task": task,
                "context": context,
                "model_profile": task.get("model_profile"),
            },
            ensure_ascii=False,
        )
        proc = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "success": False,
                "error": proc.stderr.strip() or f"executor exited {proc.returncode}",
                "capture": False,
            }
        return _parse_executor_output(proc.stdout)

    return executor


def complete_manual_task(db: SwarmDB, args) -> Dict[str, Any]:
    task = db.fetch_one("SELECT * FROM agent_tasks WHERE task_id = ?", (args.complete_task_id,))
    if not task:
        raise RuntimeError(f"task not found: {args.complete_task_id}")
    task = dict(task)
    if task["run_id"] != args.run_id:
        raise RuntimeError("task run_id does not match --run-id")
    if task["agent_id"] not in (None, args.agent):
        raise RuntimeError(f"task is claimed by another agent: {task['agent_id']}")

    model_profile = resolve_task_model_profile(db, task)
    if model_profile:
        db.execute(
            "UPDATE agent_profiles SET model_profile_id = ?, model_preference = ? WHERE agent_id = ?",
            (
                model_profile["profile_id"],
                f"{model_profile['provider']}:{model_profile['model']}",
                args.agent,
            ),
        )
        db.execute(
            "UPDATE agent_tasks SET model_profile_id = COALESCE(model_profile_id, ?) WHERE task_id = ?",
            (model_profile["profile_id"], args.complete_task_id),
        )
        db.conn.commit()

    metadata = {
        "task_type": task["task_type"],
        "task_intent": task["task_intent"],
        "required_role": task["required_role"],
        "focus_params": task["focus_params"],
        "worker_role": args.role,
        "completed_by": "agent_worker.py",
        "client_source": args.client_source,
        "model_profile": model_profile or {},
    }
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if tags:
        metadata["tags"] = tags
    if args.title:
        metadata["title"] = args.title
    if args.intent:
        metadata["intent"] = args.intent

    artifact_verification = {"ok": True, "artifacts": [], "verified": [], "failed": []}
    if args.artifact:
        artifact_verification = verify_artifacts(
            db,
            run_id=args.run_id,
            task_id=args.complete_task_id,
            agent_id=args.agent,
            artifacts=args.artifact,
        )
        if not artifact_verification["ok"]:
            fail_work_task(db, args.complete_task_id, "artifact verification failed")
            record_swarm_event(
                db,
                run_id=args.run_id,
                event_type="artifact_verification_failed",
                source=args.client_source,
                agent_id=args.agent,
                task_id=args.complete_task_id,
                content="artifact verification failed",
                metadata={"artifacts": artifact_verification["artifacts"]},
            )
            raise RuntimeError("artifact verification failed")

    entry_id = None
    if args.content.strip():
        ctx = CaptureContext(
            source=CaptureSource.TASK_RESULT,
            content=args.content,
            source_agent=args.agent,
            source_run_id=args.run_id,
            source_task_id=args.complete_task_id,
            metadata=metadata,
        )
        entry_id = capture(db, ctx, auto_classify=True)

    complete_work_task(
        db,
        args.complete_task_id,
        result_summary={
            "content": args.content[:500],
            "captured_entry_id": entry_id,
            "worker_agent": args.agent,
            "worker_role": args.role,
            "client_source": args.client_source,
            "model_profile": model_profile or {},
            "artifact_verification": artifact_verification,
        },
        token_cost=args.token_cost,
    )
    event_id = record_swarm_event(
        db,
        run_id=args.run_id,
        event_type="task_completed",
        source=args.client_source,
        agent_id=args.agent,
        task_id=args.complete_task_id,
        content=args.content or f"Task {args.complete_task_id} completed",
        metadata={
            "captured_entry_id": entry_id,
            "model_profile": model_profile or {},
            "token_cost": args.token_cost,
            "tags": tags,
            "artifact_verification": artifact_verification,
        },
    )
    return {
        "task_id": args.complete_task_id,
        "status": "completed",
        "captured_entry_id": entry_id,
        "event_id": event_id,
        "model_profile": model_profile,
        "artifact_verification": artifact_verification,
    }


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Run or claim swarm market tasks")
    parser.add_argument("--db", default=str(REPO / "swarm_knowledge.db"), help="SQLite DB path")
    parser.add_argument("--run-id", required=True, help="Swarm run_id")
    parser.add_argument("--agent", required=True, help="Agent id")
    parser.add_argument("--role", required=True, choices=["scanner", "analyst", "exploiter", "reporter", "orchestrator", "custom"])
    parser.add_argument("--executor-command", default="", help="External command used to execute each claimed task")
    parser.add_argument("--claim-only", action="store_true", help="Claim one task and print JSON without completing it")
    parser.add_argument("--complete-task-id", default="", help="Complete a previously claimed task")
    parser.add_argument("--client-source", default="agent_worker.py", help="External caller name for conversation events")
    parser.add_argument("--content", default="", help="Result content for --complete-task-id")
    parser.add_argument("--tags", default="", help="Comma-separated tags for completion capture")
    parser.add_argument("--title", default="", help="Title override for completion capture")
    parser.add_argument("--intent", default="", help="Intent override for completion capture")
    parser.add_argument("--token-cost", type=int, default=0, help="Token cost to add when completing a task")
    parser.add_argument("--artifact", action="append", default=[], help="Required artifact path to verify before completing")
    parser.add_argument("--max-tasks", type=int, default=1, help="Maximum tasks to execute")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Idle poll interval in seconds")
    args = parser.parse_args()

    db = SwarmDB(args.db)
    _ensure_schema(db)

    if args.complete_task_id:
        result = complete_manual_task(db, args)
        print(json.dumps(result, ensure_ascii=False, default=str))
        db.close()
        return 0

    executor = make_command_executor(args.executor_command) if args.executor_command else None
    worker = SwarmWorker(
        db,
        run_id=args.run_id,
        agent_id=args.agent,
        role=args.role,
        executor=executor,
        poll_interval=args.poll_interval,
    )

    if args.claim_only:
        claimed = worker.claim_once()
        print(json.dumps(claimed or {"task": None, "context": "", "model_profile": None}, ensure_ascii=False, default=str))
        db.close()
        return 0

    if executor is None:
        db.close()
        print("--executor-command is required unless --claim-only is set", file=sys.stderr)
        return 2

    result = await worker.run_loop(max_tasks=args.max_tasks)
    print(json.dumps(result, ensure_ascii=False))
    db.close()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
