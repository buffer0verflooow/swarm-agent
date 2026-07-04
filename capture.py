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
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
from src import SwarmDB, CaptureContext, CaptureSource, capture


def main():
    parser = argparse.ArgumentParser(description="Capture finding to Swarm Knowledge Base")
    parser.add_argument("--content", required=True, help="Finding content")
    parser.add_argument("--agent", default="unknown", help="Source agent name")
    parser.add_argument("--source", default="task_result",
                        choices=["task_result", "user_correction", "error_resolution",
                                 "conversation", "tool_output", "article", "discovery"])
    parser.add_argument("--phase", default="", help="Experiment phase")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
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
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    ctx = CaptureContext(
        source=source_map.get(args.source, CaptureSource.TASK_RESULT),
        content=args.content,
        source_agent=args.agent,
        metadata={
            "phase": args.phase,
            "tags": tags,
            "captured_by": "capture.py",
        },
    )

    # Override tags in classification
    entry_id = capture(db, ctx, auto_classify=True)
    if entry_id:
        # Update tags if specified
        if tags:
            db.execute(
                "UPDATE knowledge_entries SET tags = ? WHERE id = ?",
                (json.dumps(tags), entry_id),
            )
            db.conn.commit()

        print(f"CAPTURED:{entry_id[:8]}")
    else:
        print("FILTERED:low_signal")

    db.close()


if __name__ == "__main__":
    main()
