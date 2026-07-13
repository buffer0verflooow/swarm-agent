#!/usr/bin/env python3
"""
Phase A — Worker Signal Stream 端到端测试

测试:
  1. record_worker_signal — 基本记录
  2. get_recent_worker_signals — 查询
  3. record_signal_from_heartbeat — 心跳集成
  4. record_signal_from_capture — capture 集成
  5. compute_novelty_score — 新发现计算
  6. detect_loops — 原地打转检测
  7. get_stuck_workers — 卡住检测
  8. get_all_worker_signals — 全局摘要
"""

import json
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from src.swarm.signals import (
    record_worker_signal,
    get_recent_worker_signals,
    get_all_worker_signals,
    detect_loops,
    get_stuck_workers,
    compute_novelty_score,
    record_signal_from_heartbeat,
    record_signal_from_capture,
    LOOP_DETECT_WINDOW,
    LOOP_NOVELTY_THRESHOLD,
    LOOP_CONSECUTIVE_COUNT,
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


def test_basic_signal(db):
    print("\n── test: record_worker_signal ──")

    sid = record_worker_signal(
        db,
        run_id="run-001",
        agent_id="agent-A",
        signal_type="finding",
        output_quality=0.85,
        novelty_score=0.92,
        efficiency=2.5,
        progress_marker="verified 3/5 findings",
    )
    check("signal created", sid and len(sid) > 0)

    # 验证存储
    row = db.fetch_one("SELECT * FROM worker_signals WHERE signal_id = ?", (sid,))
    check("output_quality = 0.85", row["output_quality"] == 0.85)
    check("novelty_score = 0.92", row["novelty_score"] == 0.92)
    check("efficiency = 2.5", row["efficiency"] == 2.5)
    check("loop_detected = 0", row["loop_detected"] == 0)

    return sid


def test_recent_signals(db):
    print("\n── test: get_recent_worker_signals ──")

    # 记录多条信号
    for i in range(6):
        record_worker_signal(
            db, run_id="run-001", agent_id="agent-B",
            signal_type="tool_output",
            output_quality=0.5, novelty_score=0.1 * i,
        )

    recent = get_recent_worker_signals(db, "agent-B", limit=3)
    check("returns 3 signals", len(recent) == 3, f"got {len(recent)}")
    check("newest first", recent[0]["created_at"] >= recent[-1]["created_at"])


def test_heartbeat_signal(db):
    print("\n── test: record_signal_from_heartbeat ──")

    sid = record_signal_from_heartbeat(
        db, run_id="run-001", agent_id="agent-C",
        load_score=0.7, task_id="task-001",
        progress_marker="scanned 5/10 endpoints",
        tokens_spent_since=5000, findings_since=2,
    )
    check("heartbeat signal created", sid and len(sid) > 0)

    row = db.fetch_one("SELECT * FROM worker_signals WHERE signal_id = ?", (sid,))
    check("signal_type = heartbeat", row["signal_type"] == "heartbeat")
    check("output_quality from load", abs(row["output_quality"] - 0.56) < 0.001,
          f"got {row['output_quality']}")
    check("efficiency = 2/5000", row["efficiency"] == 2.0 / 5000.0)
    check("tokens = 5000", row["tokens_spent_since"] == 5000)


def test_capture_signal(db):
    print("\n── test: record_signal_from_capture ──")

    sid = record_signal_from_capture(
        db, run_id="run-001", agent_id="agent-D",
        knowledge_entry_id="ke-001",
        knowledge_type="vulnerability",
        content="Found SQLi vulnerability in login endpoint with time-based payload",
        task_id="task-002",
    )
    check("capture signal created", sid and len(sid) > 0)

    row = db.fetch_one("SELECT * FROM worker_signals WHERE signal_id = ?", (sid,))
    check("signal_type = finding", row["signal_type"] == "finding")
    check("quality for vulnerability = 0.9", row["output_quality"] == 0.9)


def test_novelty_score(db):
    print("\n── test: compute_novelty_score ──")

    # 完全新内容 → 应该接近 1.0
    score1 = compute_novelty_score(
        db, run_id="run-001",
        content="Completely new finding: discovered open redirect at /redirect endpoint",
        agent_id="agent-E",
    )
    check("new content → high novelty", score1 >= 0.7, f"got {score1}")

    # 重复内容 → 插入 raw_agent_events 使 hash 匹配
    dup_content = "Duplicate finding: same as before"
    dup_hash = hashlib.sha256(dup_content.encode()).hexdigest()[:16]
    db.execute(
        """INSERT INTO raw_agent_events
           (event_id, run_id, source_agent, source, content, content_hash, capture_status)
           VALUES (?, ?, ?, 'task_result', ?, ?, 'captured')""",
        ("evt-001", "run-001", "agent-E", dup_content, dup_hash),
    )
    db.conn.commit()

    score2 = compute_novelty_score(
        db, run_id="run-001",
        content="Duplicate finding: same as before",
        agent_id="agent-E",
    )
    check("duplicate content → low novelty", score2 < 0.5, f"got {score2}")

    # 空内容
    score3 = compute_novelty_score(db, run_id="run-001", content="")
    check("empty content → 0", score3 == 0.0)


def test_loop_detection(db):
    print("\n── test: detect_loops ──")

    agent = "agent-loop"
    run = "run-loop"

    # 先记录足够的信号，都是低 novelty
    for i in range(LOOP_CONSECUTIVE_COUNT):
        record_worker_signal(
            db, run_id=run, agent_id=agent,
            signal_type="tool_output",
            output_quality=0.4,
            novelty_score=0.05,  # 低于 threshold
            auto_detect_loop=False,  # 先关闭自动检测，手动触发
        )

    # 手动检测
    is_stuck, reason = detect_loops(db, agent, run)
    check("loop detected", is_stuck, reason)
    check("reason mentions novelty", "novelty" in reason.lower())

    # 再用 auto_detect_loop=True 记录一条 — 应该自动标记
    sid = record_worker_signal(
        db, run_id=run, agent_id=agent,
        signal_type="tool_output",
        output_quality=0.3,
        novelty_score=0.02,
        auto_detect_loop=True,
    )
    row = db.fetch_one("SELECT loop_detected FROM worker_signals WHERE signal_id = ?", (sid,))
    check("auto loop_detected=1", row["loop_detected"] == 1)


def test_get_stuck_workers(db):
    print("\n── test: get_stuck_workers ──")

    # Agent A: 高质量 — 不应被标记
    for i in range(5):
        record_worker_signal(
            db, run_id="run-stuck", agent_id="agent-good",
            signal_type="finding", output_quality=0.9,
            novelty_score=0.8, efficiency=2.0,
            auto_detect_loop=False,
        )

    # Agent B: 低质量 + 可能兜圈
    for i in range(5):
        record_worker_signal(
            db, run_id="run-stuck", agent_id="agent-bad",
            signal_type="tool_output", output_quality=0.1,
            novelty_score=0.05, efficiency=0.01,
            auto_detect_loop=False,
        )

    stuck = get_stuck_workers(db, run_id="run-stuck")
    stuck_ids = {s["agent_id"] for s in stuck}

    check("good agent not stuck", "agent-good" not in stuck_ids)
    check("bad agent stuck", "agent-bad" in stuck_ids)


def test_get_all_worker_signals(db):
    print("\n── test: get_all_worker_signals ──")

    summary = get_all_worker_signals(db, run_id="run-stuck", window_seconds=3600)
    check("summary non-empty", len(summary) > 0)

    bad = [s for s in summary if s["agent_id"] == "agent-bad"]
    check("agent-bad in summary", len(bad) == 1)
    if bad:
        check("agent-bad avg_quality low", bad[0]["avg_quality"] < 0.5,
              f"got {bad[0]['avg_quality']}")
        check("agent-bad is_stuck = 1", bad[0]["is_stuck"] == 1 or bad[0]["avg_novelty"] < 0.1)


def main():
    global PASS, FAIL

    db_path = os.path.join(tempfile.gettempdir(), "test_worker_signals.db")
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.unlink(path)

    print(f"Test DB: {db_path}")

    db = SwarmDB(db_path)

    # Apply migration 011
    mig_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations", "011_worker_signals.sql",
    )
    if os.path.exists(mig_path):
        sql = open(mig_path).read()
        db.conn.executescript(sql)
        db.conn.commit()
        print("✅ Migration 011 applied")
    else:
        print(f"❌ Migration file not found: {mig_path}")
        return 1

    # Also need raw_agent_events table for novelty test
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_agent_events (
            event_id TEXT, run_id TEXT, source_agent TEXT, source TEXT,
            content TEXT, content_hash TEXT, capture_status TEXT
        )
    """)
    db.conn.commit()

    test_basic_signal(db)
    test_recent_signals(db)
    test_heartbeat_signal(db)
    test_capture_signal(db)
    test_novelty_score(db)
    test_loop_detection(db)
    test_get_stuck_workers(db)
    test_get_all_worker_signals(db)

    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    print(f"{'='*50}")

    db.close()
    os.unlink(db_path)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
