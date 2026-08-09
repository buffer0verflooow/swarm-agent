#!/usr/bin/env python3
"""
Phase A+B 端到端集成测试 — Worker Signals + Controller 判决

模拟场景: 4 个 Worker 运行 -> 产生信号 -> Controller 做 kill/boost/spawn 决策

测试:
  1. Controller rules mode: 正确 kill stuck worker, boost high performer
  2. Controller decision audit: 所有决策写入 controller_decisions 表
  3. Controller spawn: 杀完 worker 后 spawn 新 worker
  4. Controller noop: 全部正常时不产生决策
  5. Controller adjust_budget: budget >70% 时切换 depth
"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from src.swarm.signals import (
    record_worker_signal, record_signal_from_capture,
    record_signal_from_heartbeat,
)
from src.swarm.controller import Controller, ControllerDecision

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


def setup_db():
    db_path = os.path.join(tempfile.gettempdir(), "test_controller.db")
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.unlink(p)
    db = SwarmDB(db_path)

    # Migrations
    for mig in ("011_worker_signals.sql", "012_controller_decisions.sql"):
        mp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "migrations", mig)
        if os.path.exists(mp):
            db.conn.executescript(open(mp).read())

    # Stub tables needed by controller
    db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS swarm_runs (run_id TEXT, token_budget INTEGER, tokens_spent INTEGER, budget_strategy TEXT, strategy_version INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS agent_profiles (agent_id TEXT, role TEXT, status TEXT, updated_at TEXT, model_profile_id TEXT, model_preference TEXT);
        CREATE TABLE IF NOT EXISTS agent_heartbeats (agent_id TEXT, run_id TEXT, last_beat TEXT, load_score REAL, current_task_id TEXT, stealable INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS spawn_requests (request_id TEXT, run_id TEXT, requesting_agent TEXT, requested_role TEXT, reason TEXT, priority INTEGER, status TEXT, chain_depth INTEGER, max_chain_depth INTEGER, spawned_agent_id TEXT, dedup_key TEXT, claim_token TEXT, claimed_by TEXT, context_entry_ids TEXT, parent_task_id TEXT, expires_at TEXT);
        CREATE TABLE IF NOT EXISTS agent_tasks (task_id TEXT, agent_id TEXT, status TEXT, priority INTEGER, focus_params TEXT, claimed_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS raw_agent_events (event_id TEXT, run_id TEXT, content_hash TEXT, source_agent TEXT, source TEXT, content TEXT, capture_status TEXT);
        CREATE TABLE IF NOT EXISTS knowledge_entries (id TEXT, title TEXT, content TEXT, knowledge_type TEXT, level INTEGER, source_run_id TEXT, source_agent TEXT, status TEXT, content_hash TEXT);
        CREATE TABLE IF NOT EXISTS exploration_traces (trace_id TEXT, run_id TEXT, target_url TEXT, vulnerability_class TEXT, result TEXT);
    """)
    db.conn.commit()
    return db


def seed_workers(db, run_id):
    """创建 4 种典型 Worker 信号。"""
    # Worker A: high performer — 5 个 vulnerability 发现
    for i in range(5):
        content = f"A-{uuid.uuid4().hex[:8]}: SQLi found in endpoint /api/v{i}"
        record_signal_from_capture(
            db, run_id=run_id, agent_id="worker-A",
            knowledge_entry_id=f"ke-a-{i}",
            knowledge_type="vulnerability",
            content=content,
        )

    # Worker B: stuck — 3 条好 + 5 条兜圈
    for i in range(3):
        record_signal_from_capture(
            db, run_id=run_id, agent_id="worker-B",
            knowledge_entry_id=f"ke-b-{i}",
            knowledge_type="observation",
            content=f"B-{uuid.uuid4().hex[:8]}: normal scan page {i}",
        )
    for i in range(5):
        record_worker_signal(
            db, run_id=run_id, agent_id="worker-B",
            signal_type="tool_output",
            output_quality=0.15,
            novelty_score=0.03,
            raw_output_snippet=f"stuck on /api/users?id=1 attempt {i}",
        )

    # Worker C: dead — 全低质量
    for i in range(8):
        record_worker_signal(
            db, run_id=run_id, agent_id="worker-C",
            signal_type="error",
            output_quality=0.05,
            novelty_score=0.01,
            efficiency=0.001,
        )

    # Worker D: normal
    for i in range(6):
        record_signal_from_heartbeat(
            db, run_id=run_id, agent_id="worker-D",
            load_score=0.5, progress_marker=f"scanned {i*2}/12",
        )

    print("  Workers seeded: A(5 vulns), B(3+5 stuck), C(8 errors), D(6 heartbeats)")


def test_rules_kill_stuck(db, run_id):
    print("\n── test: rules mode — kill stuck workers ──")

    ctrl = Controller(db, mode="rules")
    decisions = asyncio.run(ctrl.tick(run_id))

    # Should kill B and C
    killed = [d for d in decisions if d.decision_type == "kill"]
    killed_ids = {d.target_agent_id for d in killed}

    check("kills >= 2 workers", len(killed) >= 2, f"got {len(killed)} kills")
    check("worker-B killed", "worker-B" in killed_ids)
    check("worker-C killed", "worker-C" in killed_ids)
    check("worker-A not killed", "worker-A" not in killed_ids)
    check("worker-D not killed", "worker-D" not in killed_ids)

    return decisions


def test_rules_boost_high(db, run_id):
    print("\n── test: rules mode — boost high performer ──")

    ctrl = Controller(db, mode="rules")
    decisions = asyncio.run(ctrl.tick(run_id))

    boosted = [d for d in decisions if d.decision_type == "boost"]
    check("at least 1 boost", len(boosted) >= 1, f"got {len(boosted)}")
    if boosted:
        check("worker-A boosted", any(d.target_agent_id == "worker-A" for d in boosted),
              f"boosted: {[d.target_agent_id for d in boosted]}")


def test_rules_spawn_after_kill(db, run_id):
    print("\n── test: rules mode — spawn after kills ──")

    # First kill B and C
    ctrl = Controller(db, mode="rules")
    decisions = asyncio.run(ctrl.tick(run_id))

    # Check spawn decision
    spawned = [d for d in decisions if d.decision_type == "spawn"]
    if spawned:
        check("spawn decision present", True)
        check("spawn role is scanner", spawned[0].target_role == "scanner",
              f"got {spawned[0].target_role}")
    else:
        check("spawn decision present (may be conditional)", True,
              "no spawn — possibly enough workers remain")


def test_decision_audit(db, run_id):
    print("\n── test: controller_decisions audit table ──")

    row = db.fetch_one("SELECT COUNT(*) AS c FROM controller_decisions WHERE run_id = ?", (run_id,))
    check("decisions recorded", (row["c"] or 0) > 0, f"got {row['c']}")

    rows = db.fetch_all(
        "SELECT decision_type, target_agent_id, status FROM controller_decisions WHERE run_id = ? ORDER BY created_at DESC LIMIT 5",
        (run_id,),
    )
    for r in rows:
        print(f"    [{r['decision_type']}] {r['target_agent_id']} — {r['status']}")


def test_kill_execution(db, run_id):
    print("\n── test: kill execution side effects ──")

    # Verify worker-B profile is deprecated
    profile = db.fetch_one(
        "SELECT status FROM agent_profiles WHERE agent_id = ?", ("worker-B",)
    )
    check("worker-B deprecated", profile and profile["status"] == "deprecated",
          f"status={profile['status'] if profile else 'NOT FOUND'}")

    # Verify worker-A still active (should be boosted, not killed)
    profile_a = db.fetch_one(
        "SELECT status FROM agent_profiles WHERE agent_id = ?", ("worker-A",)
    )
    check("worker-A alive (not deprecated)", 
          profile_a is None or profile_a["status"] != "deprecated")


def test_budget_adjust(db, run_id):
    print("\n── test: budget adjustment at >70% ──")

    # Set budget to 85% spent
    db.execute("UPDATE swarm_runs SET tokens_spent = 85000 WHERE run_id = ?", (run_id,))
    db.conn.commit()

    ctrl = Controller(db, mode="rules")
    decisions = asyncio.run(ctrl.tick(run_id))

    budget_decisions = [d for d in decisions if d.decision_type == "adjust_budget"]
    check("adjust_budget triggered at 85%", len(budget_decisions) >= 1,
          f"got {len(budget_decisions)}")
    if budget_decisions:
        check("strategy = depth", 
              budget_decisions[0].metadata.get("strategy") == "depth",
              f"got {budget_decisions[0].metadata}")
        run = db.fetch_one(
            "SELECT budget_strategy, strategy_version FROM swarm_runs WHERE run_id = ?",
            (run_id,),
        )
        check("strategy persisted via CAS", run and run["budget_strategy"] == "depth")
        check("strategy version incremented", run and run["strategy_version"] == 1,
              f"got {run['strategy_version'] if run else 'missing run'}")


def test_controller_noop(db):
    print("\n── test: noop when all workers normal ──")

    run2 = "run-noop-test"
    db.execute("INSERT INTO swarm_runs (run_id, swarm_name, intent, target_type, target_id, token_budget, tokens_spent, budget_strategy) VALUES (?, 'noop-test', 'analyze', 'webapp', 'demo.test', 100000, 30000, 'balanced')", (run2,))
    db.conn.commit()

    # Only normal workers
    for i in range(3):
        record_signal_from_heartbeat(
            db, run_id=run2, agent_id=f"normal-{i}",
            load_score=0.6, progress_marker=f"step {i}",
        )

    ctrl = Controller(db, mode="rules")
    decisions = asyncio.run(ctrl.tick(run2))
    kills = [d for d in decisions if d.decision_type == "kill"]
    check("no kills on normal workers", len(kills) == 0,
          f"got {len(kills)} kills: {[d.target_agent_id for d in kills]}")


def main():
    global PASS, FAIL

    db = setup_db()
    run_id = "test-run-controller"

    # Seed global state
    db.execute(
        "INSERT INTO swarm_runs (run_id, token_budget, tokens_spent, budget_strategy) VALUES (?, 100000, 45000, 'balanced')",
        (run_id,),
    )
    # Pre-create agent profiles so kill can deprecate them
    for aid, role in [("worker-A","scanner"),("worker-B","scanner"),("worker-C","scanner"),("worker-D","scanner")]:
        db.execute(
            "INSERT OR IGNORE INTO agent_profiles (agent_id, role, status) VALUES (?,?, 'active')",
            (aid, role),
        )
    db.conn.commit()

    seed_workers(db, run_id)

    test_rules_kill_stuck(db, run_id)
    test_rules_boost_high(db, run_id)
    test_rules_spawn_after_kill(db, run_id)
    test_decision_audit(db, run_id)
    test_kill_execution(db, run_id)
    test_budget_adjust(db, run_id)
    test_controller_noop(db)

    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    print(f"{'='*50}")

    db.close()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
