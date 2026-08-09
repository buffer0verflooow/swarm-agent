#!/usr/bin/env python3
"""
一键知识捕获 — 给子 agent 用的极简入口。

用法 (在子 agent 中):
  python3 /home/pwn/workspace/research/swarm-knowledge/capture.py \\
    --content "发现内容" \\
    --agent "agent-name" \\
    --source "task_result" \\
    --phase "1" \\
    --tags "auth,api,jwt"

子 agent 在完成任务后调用此脚本，自动将发现写入知识库。
"""

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from src import SwarmDB, CaptureContext, CaptureSource, capture


def main():
    parser = argparse.ArgumentParser(description="Capture finding to Swarm Knowledge Base")
    parser.add_argument("--content", required=True, help="Finding content")
    parser.add_argument("--agent", default="unknown", help="Source agent name")
    parser.add_argument("--source", default="task_result",
                        choices=["task_result", "user_correction", "error_resolution",
                                 "conversation", "tool_output", "article", "discovery"])
    parser.add_argument("--run-id", default="", help="Associated swarm run_id")
    parser.add_argument("--task-id", default="", help="Associated agent task_id")
    parser.add_argument("--intent", default="", help="Knowledge intent override")
    parser.add_argument("--title", default="", help="Entry title override")
    parser.add_argument("--phase", default="", help="Experiment phase")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--force-capture", action="store_true", help="Force promotion into knowledge_entries")
    parser.add_argument("--db", default=str(REPO / "swarm_knowledge.db"), help="DB path")
    args = parser.parse_args()

    source_map = {
        "task_result": CaptureSource.TASK_RESULT,
        "user_correction": CaptureSource.USER_CORRECTION,
        "error_resolution": CaptureSource.ERROR_RESOLUTION,
        "conversation": CaptureSource.CONVERSATION,
        "tool_output": CaptureSource.TOOL_OUTPUT,
        "article": CaptureSource.ARTICLE,
        "discovery": CaptureSource.DISCOVERY,
    }

    db = SwarmDB(args.db)
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_entries'"):
        db.init()
    if not db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name='raw_agent_events'"):
        db.init()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    ctx = CaptureContext(
        source=source_map.get(args.source, CaptureSource.TASK_RESULT),
        content=args.content,
        source_agent=args.agent,
        source_run_id=args.run_id or None,
        source_task_id=args.task_id or None,
        metadata={
            "phase": args.phase,
            "tags": tags,
            "force_capture": args.force_capture,
            **({"intent": args.intent} if args.intent else {}),
            **({"title": args.title} if args.title else {}),
            "captured_by": "capture.py",
        },
    )

    entry_id = capture(db, ctx, auto_classify=True)
    if entry_id:
        print(f"CAPTURED:{entry_id[:8]}")
    else:
        content_hash = hashlib.sha256(args.content.encode()).hexdigest()[:16]
        raw = db.fetch_one(
            """SELECT event_id FROM raw_agent_events
               WHERE content_hash = ? AND source_agent = ?
               ORDER BY created_at DESC LIMIT 1""",
            (content_hash, args.agent),
        )
        suffix = f":RAW:{raw['event_id'][:8]}" if raw else ""
        print(f"FILTERED:low_signal{suffix}")

    db.close()


if __name__ == "__main__":
    main()
