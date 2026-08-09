#!/usr/bin/env python3
"""
Exploration Trace CLI — Agent 调用的探索轨迹记录工具

每个 Agent 完成一次测试后调用此脚本记录探索轨迹。
Phase A: 不做语义归一化，只记录 literal URL。

用法:
    python3 ~/workspace/research/swarm-knowledge/exploration_trace.py \
        --target-url "https://api.target.com/users/123" \
        --method GET \
        --vuln-class "IDOR" \
        --result "not_found" \
        --depth "medium" \
        --agent "scanner-abc123" \
        --run-id "run-001" \
        --notes "Tested role escalation via modified JWT claims"

    # 与 capture.py 配合使用（先 capture 发现，再 trace 探索路径）
    python3 ~/workspace/research/swarm-knowledge/capture.py \
        --content "found IDOR..." --agent "scanner-abc123" --source task_result \
        --tags "confirmed,high,idor" --force-capture
    python3 ~/workspace/research/swarm-knowledge/exploration_trace.py \
        --target-url "https://api.target.com/users/123" \
        --method GET --vuln-class "IDOR" --result "found" \
        --depth "deep" --agent "scanner-abc123" --run-id "run-001" \
        --finding-id "<CAPTURED_ID>"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import SwarmDB, record_trace

def main():
    ap = argparse.ArgumentParser(description="Exploration Trace CLI (Phase A)")
    ap.add_argument("--target-url", required=True, help="被测试的 literal URL")
    ap.add_argument("--method", default="GET", choices=["GET","POST","PUT","DELETE","PATCH","HEAD","OPTIONS"])
    ap.add_argument("--vuln-class", required=True, help="漏洞类型: IDOR, SQLi, XSS, auth_bypass, ...")
    ap.add_argument("--result", required=True,
                    choices=["found","not_found","blocked","error","inconclusive"])
    ap.add_argument("--depth", default="shallow",
                    choices=["shallow","medium","deep"])
    ap.add_argument("--agent", default="unknown", help="记录此 trace 的 agent_id")
    ap.add_argument("--run-id", default="", help="swarm run ID")
    ap.add_argument("--task-id", default="", help="关联的 task_id")
    ap.add_argument("--finding-id", default="", help="如果 result=found，关联的 knowledge_entry id")
    ap.add_argument("--notes", default="", help="附加说明")

    ap.add_argument("--db", default="", help="SQLite DB path (default: swarm_knowledge.db)")

    args = ap.parse_args()

    db_path = args.db or str(Path(__file__).resolve().parent.parent / "swarm_knowledge.db")
    db = SwarmDB(db_path)

    trace_id = record_trace(
        db,
        run_id=args.run_id,
        task_id=args.task_id,
        agent_id=args.agent,
        target_url=args.target_url,
        method=args.method,
        vulnerability_class=args.vuln_class,
        result=args.result,
        finding_id=args.finding_id,
        depth=args.depth,
        notes=args.notes,
    )

    print(f"TRACED:{trace_id[:8]}")
    db.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
