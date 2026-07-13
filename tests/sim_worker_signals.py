#!/usr/bin/env python3
"""
Worker Signal Quality Simulator — 模拟 3 种 Worker 行为，看 Controller 能观察到什么。

模拟场景:
  Worker A (high-performer): 持续发现漏洞，高质量输出
  Worker B (stuck): 前几轮正常，之后在同一个 URL 上兜圈
  Worker C (error-prone): 一直返回低质量/重复内容
"""

import hashlib
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from src.swarm.signals import (
    record_worker_signal, get_stuck_workers, detect_loops,
    get_all_worker_signals, record_signal_from_capture,
    record_signal_from_heartbeat,
)

DB_PATH = "/tmp/signal_sim.db"

def fresh_db():
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH + suffix
        if os.path.exists(path):
            os.unlink(path)
    db = SwarmDB(DB_PATH)
    # 011
    sql = open("migrations/011_worker_signals.sql").read()
    db.conn.executescript(sql)
    # 012 — knowledge_entries stub for capture signals
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            id TEXT, title TEXT, content TEXT, knowledge_type TEXT,
            level INTEGER, source_run_id TEXT, source_agent TEXT,
            status TEXT, created_at TEXT, content_hash TEXT
        )
    """)
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_agent_events (
            event_id TEXT, run_id TEXT, source_agent TEXT, source TEXT,
            content TEXT, content_hash TEXT, capture_status TEXT
        )
    """)
    db.conn.commit()
    return db

run_id = "sim-run-001"

def sim_worker_a(db):
    """High performer: 持续产出高质量发现"""
    findings = [
        ("IDOR in user profile endpoint", "vulnerability"),
        ("SQL blind injection in search", "vulnerability"),
        ("Sensitive info leak in /debug", "vulnerability"),
        ("JWT none algorithm accepted", "technique"),
        ("Open redirect in /logout", "vulnerability"),
    ]
    for i, (desc, ktype) in enumerate(findings):
        record_signal_from_capture(
            db, run_id=run_id, agent_id="worker-A",
            knowledge_entry_id=f"ke-a-{i}",
            knowledge_type=ktype,
            content=f"A-{uuid.uuid4().hex[:8]}: {desc} with unique payload {i}",
        )
        print(f"  Worker A: found [{ktype}] {desc[:50]}...")

def sim_worker_b(db):
    """Gets stuck after initial work"""
    # 前 3 轮正常
    for i in range(3):
        record_signal_from_capture(
            db, run_id=run_id, agent_id="worker-B",
            knowledge_entry_id=f"ke-b-{i}",
            knowledge_type="observation",
            content=f"B-{uuid.uuid4().hex[:8]}: scanning endpoint /api/users page {i}",
        )
        print(f"  Worker B: [good] scanned /api/users page {i}")
    
    # 后面 5 轮兜圈 — 全是相似输出
    base = f"B-fixed: scanning /api/users?id=1 — same response as before"
    for i in range(5):
        record_worker_signal(
            db, run_id=run_id, agent_id="worker-B",
            signal_type="tool_output",
            output_quality=0.2,
            novelty_score=0.05,
            raw_output_snippet=f"{base} (attempt {i+4})",
            auto_detect_loop=True,
        )
        print(f"  Worker B: [stuck] same /api/users? attempt {i+4}")

def sim_worker_c(db):
    """Always low quality"""
    for i in range(8):
        record_worker_signal(
            db, run_id=run_id, agent_id="worker-C",
            signal_type="tool_output",
            output_quality=0.1,
            novelty_score=0.03,
            efficiency=0.01,
            raw_output_snippet=f"C-error: connection timeout attempt {i}",
            auto_detect_loop=True,
        )
        if i == 0:
            print(f"  Worker C: [error] connection timeout...")

def sim_worker_d(db):
    """Normal worker, mixed quality"""
    for i in range(6):
        quality = 0.8 if i % 2 == 0 else 0.4
        novelty = 0.7 if i < 3 else 0.15
        record_worker_signal(
            db, run_id=run_id, agent_id="worker-D",
            signal_type="tool_output",
            output_quality=quality,
            novelty_score=novelty,
            efficiency=1.5 if quality > 0.5 else 0.3,
            progress_marker=f"scanned {i*2}/12 endpoints",
        )
    print(f"  Worker D: mixed — 6 rounds, quality avg ~0.6")

def main():
    print("=" * 60)
    print("Worker Signal Quality Simulator")
    print("=" * 60)

    db = fresh_db()
    
    # Simulate
    print("\n── Simulating Workers ──")
    sim_worker_a(db)
    sim_worker_b(db)
    sim_worker_c(db)
    sim_worker_d(db)

    # Analyze
    print("\n── Controller's View ──")

    # 1. Stuck detection
    stuck = get_stuck_workers(db, run_id=run_id)
    print(f"\n🔴 Stuck workers: {len(stuck)}")
    for s in stuck:
        print(f"  {s['agent_id']}: {s['reasons']} "
              f"(q={s['avg_quality']}, nov={s['avg_novelty']}, eff={s['avg_efficiency']})")

    # 2. Loop detection
    for aid in ["worker-B", "worker-C"]:
        is_stuck, reason = detect_loops(db, aid, run_id)
        status = "🔴 STUCK" if is_stuck else "✅ OK"
        print(f"\n{status} {aid}: {reason}")

    # 3. Global summary
    summary = get_all_worker_signals(db, run_id=run_id, window_seconds=3600)
    print(f"\n── Global Worker Summary ──")
    print(f"{'Worker':12s} {'Signals':>7s} {'AvgQ':>6s} {'AvgNov':>7s} {'AvgEff':>7s} {'Stuck':>5s} {'Progress'}")
    print("-" * 70)
    for s in summary:
        print(f"{s['agent_id']:12s} {s['signal_count']:>7d} "
              f"{s['avg_quality']:>6.3f} {s['avg_novelty']:>7.3f} "
              f"{s['avg_efficiency']:>7.3f} {s['is_stuck']:>5d} "
              f"{s['latest_progress'] or '-'}")

    # 4. What Controller would decide
    print(f"\n── Controller's Hypothetical Decisions ──")
    for s in summary:
        aid = s['agent_id']
        is_stuck, loop_reason = detect_loops(db, aid, run_id)
        stuck_flag = is_stuck or s['is_stuck']
        if stuck_flag:
            print(f"  🗑️  KILL {aid}: stuck={'loop' if is_stuck else 'aggregate'}, quality={s['avg_quality']:.2f}")
            if is_stuck:
                print(f"     → {loop_reason}")
            print(f"     → spawn replacement for same role")
        elif s['avg_quality'] > 0.7 and s['avg_novelty'] > 0.5:
            print(f"  🚀  BOOST {aid}: quality={s['avg_quality']:.2f}, novelty={s['avg_novelty']:.2f}")
            print(f"     → increase budget allocation")
        elif s['avg_efficiency'] < 0.5 and s['signal_count'] > 3:
            print(f"  ⚠️  WARN {aid}: efficiency={s['avg_efficiency']:.3f}, monitor closely")
        else:
            print(f"  ✅  KEEP {aid}: normal operation")

    db.close()
    print(f"\n{'='*60}")
    print("Simulation complete. DB at:", DB_PATH)

if __name__ == "__main__":
    main()
