#!/usr/bin/env python3
"""
Phase A — Exploration Traces 端到端测试

测试覆盖:
  1. migration 010 是否能正确应用
  2. record_trace() — 基本记录
  3. get_explored_for_target() — 查询
  4. get_exploration_summary() — 统计
  5. get_exhausted_paths() — 穷尽检测
  6. build_exploration_context() — Agent 上下文生成
  7. get_unexplored_hints() — 未探索建议
  8. exploration_trace CLI — 命令行调用
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from src.swarm.exploration import (
    record_trace,
    get_explored_for_target,
    get_exploration_summary,
    get_exhausted_paths,
    build_exploration_context,
    get_unexplored_hints,
)

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def test_basic_trace(db):
    print("\n── test: record_trace ──")

    tid1 = record_trace(
        db,
        run_id="test-run-001",
        task_id="task-001",
        agent_id="agent-scanner-01",
        target_url="https://api.target.com/users/123",
        method="GET",
        vulnerability_class="IDOR",
        result="not_found",
        depth="shallow",
        notes="Tried changing user ID to 124, same response",
    )
    check("trace created", tid1 is not None and len(tid1) > 0)

    tid2 = record_trace(
        db,
        run_id="test-run-001",
        task_id="task-002",
        agent_id="agent-scanner-01",
        target_url="https://api.target.com/users/123",
        method="GET",
        vulnerability_class="IDOR",
        result="not_found",
        depth="medium",
        notes="Tried with JWT role escalation, same response",
    )
    check("second trace same path", tid1 != tid2)

    tid3 = record_trace(
        db,
        run_id="test-run-001",
        task_id="task-003",
        agent_id="agent-analyst-01",
        target_url="https://api.target.com/login",
        method="POST",
        vulnerability_class="SQLi",
        result="found",
        depth="deep",
        notes="Blind SQLi confirmed with time-based payload",
        finding_id="finding-001",
    )
    check("found trace with finding_id", tid3 is not None)

    return tid1, tid2, tid3


def test_explored_for_target(db):
    print("\n── test: get_explored_for_target ──")

    results = get_explored_for_target(db, "https://api.target.com/users/123")
    check("found 2 traces for /users/123", len(results) == 2, f"got {len(results)}")
    check("both are IDOR not_found",
          all(r["vulnerability_class"] == "idor" and r["result"] == "not_found" for r in results))

    results_idor = get_explored_for_target(
        db, "https://api.target.com/users/123", vulnerability_class="idor"
    )
    check("filter by vuln_class IDOR", len(results_idor) == 2)

    results_none = get_explored_for_target(db, "https://nonexistent.example.com/nope")
    check("no results for unknown URL", len(results_none) == 0)


def test_exploration_summary(db):
    print("\n── test: get_exploration_summary ──")

    summary = get_exploration_summary(db, run_id="test-run-001")
    check("total_traces = 3", summary["total_traces"] == 3, f"got {summary['total_traces']}")
    check("unique_targets = 2", summary["unique_targets"] == 2)
    check("unique_coverage = 2", summary["unique_coverage"] == 2, f"got {summary['unique_coverage']}")
    check("by_result has found/not_found", "found" in summary["by_result"] and "not_found" in summary["by_result"])


def test_exhausted_detection(db):
    print("\n── test: get_exhausted_paths ──")

    # Add a 3rd not_found for users/123 IDOR to trigger exhaustion
    for i in range(1):
        record_trace(
            db,
            run_id="test-run-001",
            task_id=f"task-exhaust-{i}",
            agent_id="agent-scanner-01",
            target_url="https://api.target.com/users/123",
            method="GET",
            vulnerability_class="IDOR",
            result="not_found",
            depth="shallow",
            notes=f"Re-test attempt {i+3}",
        )

    exhausted = get_exhausted_paths(db, run_id="test-run-001")
    check("exhausted paths found", len(exhausted) >= 1)
    if exhausted:
        check("users/123 IDOR is exhausted",
              any("users/123" in e["target_url"] and e["vulnerability_class"] == "idor"
                  for e in exhausted))

    # Verify threshold=5 should NOT flag (only 3 not_found)
    exhausted5 = get_exhausted_paths(db, run_id="test-run-001", threshold=5)
    check("threshold=5 returns none", len(exhausted5) == 0,
          f"got {len(exhausted5)} exhausted at threshold=5")


def test_build_context(db):
    print("\n── test: build_exploration_context ──")

    ctx = build_exploration_context(db, run_id="test-run-001")
    check("context is non-empty", len(ctx) > 0)
    check("context mentions '蜂群探索记忆'", "蜂群探索记忆" in ctx)
    check("context mentions IDOR", "IDOR" in ctx or "idor" in ctx)
    check("context mentions exhausted path", "已穷尽" in ctx)
    check("context mentions found", "已有发现" in ctx)
    print(f"  Context preview ({len(ctx)} chars):")
    for line in ctx.split("\n")[:8]:
        print(f"    {line}")


def test_unexplored_hints(db):
    print("\n── test: get_unexplored_hints ──")

    known_endpoints = [
        "https://api.target.com/users/123",
        "https://api.target.com/login",
        "https://api.target.com/admin",
    ]

    # With 3 endpoints, verify hints contain expected content
    hints = get_unexplored_hints(
        db, run_id="test-run-001", known_endpoints=known_endpoints
    )
    # Default limit is 20, but we have 3 endpoints × 10 vuln classes = 30
    # Let's check the first endpoint appears
    print(f"  Hints preview: {hints[:100]}")
    check("hints non-empty", len(hints) > 0)
    check("hints mentions '未探索路径建议'", "未探索路径建议" in hints)
    # With 3 endpoints, /admin entries should be in the first 20
    check("hints includes known endpoint", "users/123" in hints or "login" in hints or "admin" in hints)


def test_cli(db_path):
    print("\n── test: exploration_trace CLI ──")

    import subprocess

    cli_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exploration_trace.py")

    result = subprocess.run(
        [
            sys.executable, cli_path,
            "--db", db_path,
            "--target-url", "https://cli-test.example.com/api/health",
            "--method", "GET",
            "--vuln-class", "information_disclosure",
            "--result", "not_found",
            "--depth", "shallow",
            "--agent", "cli-test-agent",
            "--run-id", "test-run-001",
            "--notes", "CLI integration test",
        ],
        capture_output=True, text=True, timeout=10,
    )
    check("CLI exit code 0", result.returncode == 0, f"exit={result.returncode}, stderr={result.stderr[:200]}")
    check("CLI outputs TRACED:", "TRACED:" in result.stdout, f"stdout={result.stdout[:100]}")


def test_context_empty_run(db):
    print("\n── test: build_exploration_context empty run ──")

    ctx = build_exploration_context(db, run_id="nonexistent-run")
    check("empty context for unknown run", ctx == "")


# ── runner ──

def main():
    global PASS, FAIL

    # Use in-memory DB for tests — always fresh
    db_path = os.path.join(tempfile.gettempdir(), "test_exploration_traces.db")

    # Clean up stale file from previous runs
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.unlink(path)

    print(f"Test DB: {db_path}")

    db = SwarmDB(db_path)

    # Apply migration 010
    mig_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations", "010_exploration_traces.sql")
    if os.path.exists(mig_path):
        sql = open(mig_path).read()
        # Use executescript to handle multi-statement SQL with CHECK constraints
        db.conn.executescript(sql)
        db.conn.commit()
        print("✅ Migration 010 applied")
    else:
        print(f"❌ Migration file not found: {mig_path}")
        return 1

    # Run tests
    test_basic_trace(db)
    test_explored_for_target(db)
    test_exploration_summary(db)
    test_exhausted_detection(db)
    test_build_context(db)
    test_unexplored_hints(db)
    test_context_empty_run(db)
    test_cli(db_path)

    # Summary
    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    print(f"{'='*50}")

    db.close()
    os.unlink(db_path)

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
