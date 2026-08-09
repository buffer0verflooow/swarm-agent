"""Tests for opt-in action-value scheduling (migration 014 + src/swarm/action_value.py).

Run: python -m pytest tests/test_action_value.py -q   (from repo root)
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import SwarmDB
from src.swarm import action_value as av


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture()
def db(tmp_path):
    database = SwarmDB(str(tmp_path / "test.db"))
    assert database.init(), "migrations should apply (incl. 014_action_value)"
    yield database
    database.close()


def make_run(db, run_id: str, config: dict | None = None) -> str:
    db.execute(
        """INSERT INTO swarm_runs
           (run_id, swarm_name, intent, target_type, target_id, status, config)
           VALUES (?, ?, 'analyze', 'domain', 'example.com', 'running', ?)""",
        (run_id, f"run-{run_id}", json.dumps(config or {})),
    )
    db.conn.commit()
    return run_id


def add_pending(db, run_id, role, task_type, priority, *, intent="", focus=None,
                parent_task_id=None) -> str:
    task_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO agent_tasks
           (task_id, run_id, parent_task_id, task_type, task_intent, focus_params,
            iteration, status, required_role, priority)
           VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', ?, ?)""",
        (task_id, run_id, parent_task_id, task_type, intent,
         json.dumps(focus or {}), role, priority),
    )
    db.conn.commit()
    return task_id


def add_finished(db, run_id, role, task_type, *, informative, tokens, intent=""):
    """Insert a completed/failed historical task for the given fingerprint."""
    task_id = str(uuid.uuid4())
    summary = {"captured_entry_id": str(uuid.uuid4())} if informative else {"content": "prose only"}
    db.execute(
        """INSERT INTO agent_tasks
           (task_id, run_id, task_type, task_intent, focus_params, iteration,
            status, required_role, priority, result_summary, token_cost)
           VALUES (?, ?, ?, ?, '{}', 1, 'completed', ?, 50, ?, ?)""",
        (task_id, run_id, task_type, intent, role, json.dumps(summary), tokens),
    )
    db.conn.commit()
    return task_id


def priority_of(db, task_id: str) -> int:
    return int(db.fetch_one("SELECT priority FROM agent_tasks WHERE task_id = ?", (task_id,))["priority"])


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_default_policy_is_a_noop(db):
    """Without scheduler_policy=value, nothing is re-ranked or logged."""
    run_id = make_run(db, "r-default")  # no config
    high = add_pending(db, run_id, "analyst", "analyze", priority=80)
    low = add_pending(db, run_id, "reporter", "report", priority=40)

    assert av.maybe_rescore_pending(db, run_id) is None
    assert priority_of(db, high) == 80  # untouched
    assert priority_of(db, low) == 40
    rows = db.fetch_all("SELECT * FROM scheduler_decisions WHERE run_id = ?", (run_id,))
    assert rows == []
    # base_priority never written under the default policy
    assert db.fetch_one("SELECT base_priority FROM agent_tasks WHERE task_id = ?", (high,))["base_priority"] is None


def test_cold_start_preserves_designer_priority(db):
    """No history + no exploration => value ordering tracks the hand priority."""
    run_id = make_run(db, "r-cold", {"scheduler_policy": "value", "exploration_ratio": 0.0})
    hi = add_pending(db, run_id, "exploiter", "exploit", priority=90)
    mid = add_pending(db, run_id, "analyst", "analyze", priority=80)
    lo = add_pending(db, run_id, "reporter", "report", priority=55)

    out = av.rescore_pending(db, run_id, exploration_ratio=0.0)
    assert out["rescored"] == 3 and out["explore"] == 0
    # Same rank order as the static priority (higher prior -> higher effective).
    assert priority_of(db, hi) > priority_of(db, mid) > priority_of(db, lo)
    # Original priority preserved as base_priority for future feature use.
    assert db.fetch_one("SELECT base_priority FROM agent_tasks WHERE task_id = ?", (hi,))["base_priority"] == 90


def test_learning_flips_a_low_value_high_priority_task(db):
    """A cheap, historically-informative role beats an expensive, fruitless one
    even though the latter has the higher hand-assigned priority."""
    hist = make_run(db, "r-hist")
    for _ in range(5):
        add_finished(db, hist, "analyst", "analyze", informative=True, tokens=2000)
        add_finished(db, hist, "exploiter", "exploit", informative=False, tokens=30000)

    run_id = make_run(db, "r-live", {"scheduler_policy": "value", "exploration_ratio": 0.0})
    good = add_pending(db, run_id, "analyst", "analyze", priority=80)   # lower static priority
    bad = add_pending(db, run_id, "exploiter", "exploit", priority=90)  # higher static priority

    av.maybe_rescore_pending(db, run_id)
    assert priority_of(db, good) > priority_of(db, bad), (
        "learned success + low cost should outrank a higher static priority"
    )

    decisions = {
        d["task_id"]: d
        for d in db.fetch_all("SELECT * FROM scheduler_decisions WHERE run_id = ?", (run_id,))
    }
    good_feat = json.loads(decisions[good]["features"])
    bad_feat = json.loads(decisions[bad]["features"])
    assert good_feat["success_probability"] > bad_feat["success_probability"]
    assert bad_feat["cost"] > good_feat["cost"]


def test_unlock_boosts_a_task_with_pending_children(db):
    """Two identical-fingerprint tasks: the one that unlocks downstream work wins."""
    run_id = make_run(db, "r-unlock", {"scheduler_policy": "value", "exploration_ratio": 0.0})
    blocker = add_pending(db, run_id, "scanner", "scan", priority=60)
    plain = add_pending(db, run_id, "scanner", "scan", priority=60)
    # Three pending children depend on `blocker`.
    for _ in range(3):
        add_pending(db, run_id, "analyst", "analyze", priority=50, parent_task_id=blocker)

    av.rescore_pending(db, run_id, exploration_ratio=0.0)
    assert priority_of(db, blocker) > priority_of(db, plain)


def test_exploration_quota_and_decision_logging(db):
    """A non-zero exploration ratio marks explore tasks and logs every candidate."""
    run_id = make_run(db, "r-explore", {"scheduler_policy": "value", "exploration_ratio": 0.5})
    ids = [
        add_pending(db, run_id, "analyst", "analyze", priority=50 + i, intent=f"i{i}")
        for i in range(4)
    ]

    out = av.maybe_rescore_pending(db, run_id)
    assert out is not None and out["rescored"] == 4
    assert out["explore"] >= 1

    rows = db.fetch_all("SELECT mode FROM scheduler_decisions WHERE run_id = ?", (run_id,))
    assert len(rows) == 4  # one decision per candidate
    modes = {r["mode"] for r in rows}
    assert "explore" in modes and "exploit" in modes


def test_reticks_do_not_duplicate_decisions(db):
    """Repeated ticks replace the per-(run,task,generation) decision in place."""
    run_id = make_run(db, "r-idem", {"scheduler_policy": "value", "exploration_ratio": 0.0})
    add_pending(db, run_id, "analyst", "analyze", priority=70)
    add_pending(db, run_id, "reporter", "report", priority=40)

    av.maybe_rescore_pending(db, run_id)
    av.maybe_rescore_pending(db, run_id)
    av.maybe_rescore_pending(db, run_id)

    count = db.fetch_one(
        "SELECT COUNT(*) AS c FROM scheduler_decisions WHERE run_id = ?", (run_id,)
    )["c"]
    assert count == 2  # two tasks, one decision each — not 6


def test_set_scheduler_policy_toggle(db):
    run_id = make_run(db, "r-toggle")
    assert av.scheduler_policy_for_run(db, run_id) == ("priority", 0.2)
    av.set_scheduler_policy(db, run_id, "value", exploration_ratio=0.3)
    assert av.scheduler_policy_for_run(db, run_id) == ("value", 0.3)
