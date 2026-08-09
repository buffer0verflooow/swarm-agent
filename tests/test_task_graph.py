"""Tests for the domain-agnostic task graph layer (migration 015 +
src/swarm/task_graph.py).

Run: .venv/bin/python -m pytest tests/test_task_graph.py -q   (from repo root)

Covers:
  * graph lifecycle (create / add nodes / validation of unknown deps)
  * dependency-gated publishing into the work market
  * acceptance evaluation (metric ops, required vs optional)
  * receipt-verified evidence chain
  * rolling admission (spawn subtask growth)
  * goal-level progress view
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import SwarmDB
from src.swarm import task_graph as tg
from src.swarm.work_queue import claim_work_tasks, poll_work_tasks


@pytest.fixture()
def db(tmp_path):
    database = SwarmDB(str(tmp_path / "test.db"))
    assert database.init(), "migrations should apply (incl. 015_task_graph)"
    yield database
    database.close()


def make_run(db, run_id: str | None = None) -> str:
    run_id = run_id or str(uuid.uuid4())
    db.execute(
        """INSERT INTO swarm_runs
           (run_id, swarm_name, intent, target_type, target_id, status)
           VALUES (?, 'test-swarm', 'analyze', 'webapp', 'demo.test', 'running')""",
        (run_id,),
    )
    db.conn.commit()
    return run_id


# --------------------------------------------------------------------------- #
# Graph lifecycle
# --------------------------------------------------------------------------- #

def test_create_graph_and_add_nodes(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(
        db, run_id, goal="Map attack surface", domain="web", strategy="deterministic"
    )
    g = tg.get_graph(db, graph_id)
    assert g["goal"] == "Map attack surface"
    assert g["domain"] == "web"
    assert g["total_nodes"] == 0

    tg.add_task_node(
        db, graph_id, task_key="recon:dns", goal="Enumerate DNS",
        role="scanner", task_type="scan",
        acceptance_criteria=[{"metric": "output_nonempty", "op": "==", "value": True}],
        tool_allowlist=["dns", "http"],
    )
    tg.add_task_node(
        db, graph_id, task_key="recon:http", goal="Fingerprint HTTP",
        role="scanner", task_type="scan", depends_on=["recon:dns"],
        priority=70,
    )
    nodes = tg.get_graph_nodes(db, graph_id)
    assert len(nodes) == 2
    # ordered by phase ASC, priority DESC -> http (70) before dns (50)
    assert nodes[0]["task_key"] == "recon:http"
    assert nodes[0]["status"] == "pending"
    assert json.loads(nodes[0]["depends_on"]) == ["recon:dns"]
    assert json.loads(nodes[1]["tool_allowlist"]) == ["dns", "http"]
    assert json.loads(nodes[1]["depends_on"]) == []
    g = tg.get_graph(db, graph_id)
    assert g["total_nodes"] == 2


def test_add_node_rejects_unknown_dependency(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g")
    with pytest.raises(ValueError, match="depends on unknown keys"):
        tg.add_task_node(db, graph_id, task_key="child", goal="c", depends_on=["ghost"])


def test_create_graph_requires_run_and_goal(db):
    with pytest.raises(ValueError):
        tg.create_task_graph(db, "", goal="x")


# --------------------------------------------------------------------------- #
# Dependency-gated publishing
# --------------------------------------------------------------------------- #

def test_publish_ready_nodes_dependency_gated(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g", domain="web")
    tg.add_task_node(db, graph_id, task_key="root", goal="root scan",
                     role="scanner", task_type="scan", priority=80)
    tg.add_task_node(db, graph_id, task_key="child", goal="child analyze",
                     role="analyst", task_type="analyze", depends_on=["root"])

    published = tg.publish_ready_nodes(db, graph_id)
    assert len(published) == 1
    assert published[0]["task_key"] == "root"

    # child must not be claimable yet
    pending = poll_work_tasks(db, run_id=run_id, status="pending", limit=10)
    keys = [t["task_key"] for t in pending if t.get("task_key")]
    assert "child" not in keys

    # complete root via the market path -> child unlocks
    root_task = db.fetch_one(
        "SELECT task_id FROM agent_tasks WHERE task_key = 'root'", ()
    )
    tg.evaluate_acceptance(
        db, root_task["task_id"],
        result_summary={"metrics": {"output_nonempty": True, "verified_address_count": 3}},
    )
    from src.swarm.work_queue import complete_work_task
    complete_work_task(db, root_task["task_id"],
                       result_summary={"metrics": {"output_nonempty": True}})
    db.execute(
        "UPDATE task_graph_nodes SET status = 'completed' WHERE graph_id = ? AND task_key = 'root'",
        (graph_id,),
    )
    db.conn.commit()

    published2 = tg.publish_ready_nodes(db, graph_id)
    assert len(published2) == 1
    assert published2[0]["task_key"] == "child"


def test_claim_graph_task_via_market(db):
    """Graph tasks are claimable by role through the existing market."""
    run_id = make_run(db)
    # agent must exist for the FK on agent_tasks.agent_id
    from src.swarm.lifecycle import AgentLifecycle
    AgentLifecycle(db, "agent-1", run_id).register(role="scanner", capabilities=["mock_scan"])

    graph_id = tg.create_task_graph(db, run_id, goal="g")
    tg.add_task_node(db, graph_id, task_key="scan1", goal="scan x",
                     role="scanner", task_type="scan", priority=90)
    tg.publish_ready_nodes(db, graph_id)

    claimed = claim_work_tasks(db, run_id=run_id, agent_id="agent-1", role="scanner")
    assert len(claimed) == 1
    assert claimed[0]["task_key"] == "scan1"
    assert claimed[0]["graph_id"] == graph_id
    assert claimed[0]["acceptance_status"] == "pending"
    assert json.loads(claimed[0]["tool_allowlist"]) == []


# --------------------------------------------------------------------------- #
# Acceptance evaluation
# --------------------------------------------------------------------------- #

def test_acceptance_metric_ops(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g")
    tg.add_task_node(
        db, graph_id, task_key="t1", goal="check",
        acceptance_criteria=[
            {"metric": "output_nonempty", "op": "==", "value": True, "required": True},
            {"metric": "verified_evidence_count", "op": ">=", "value": 2, "required": True},
            {"metric": "note", "op": "contains", "value": "ok", "required": False},
        ],
    )
    published = tg.publish_ready_nodes(db, graph_id)
    task_id = published[0]["task_id"]

    # pass
    res = tg.evaluate_acceptance(
        db, task_id,
        result_summary={"metrics": {"output_nonempty": True, "verified_evidence_count": 3,
                                    "note": "all ok"}},
    )
    assert res["accepted"] is True
    assert res["status"] == "accepted"
    assert res["unmet"] == []

    # fail on required metric
    res2 = tg.evaluate_acceptance(
        db, task_id,
        result_summary={"metrics": {"output_nonempty": True, "verified_evidence_count": 0}},
    )
    assert res2["accepted"] is False
    assert res2["status"] == "rejected"
    assert any("verified_evidence_count" in u for u in res2["unmet"])

    # optional metric failing does not reject
    res3 = tg.evaluate_acceptance(
        db, task_id,
        result_summary={"metrics": {"output_nonempty": True, "verified_evidence_count": 5,
                                    "note": "bad"}},
    )
    assert res3["accepted"] is True


def test_acceptance_dotted_metric(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g")
    tg.add_task_node(
        db, graph_id, task_key="t1", goal="check",
        acceptance_criteria=[{"metric": "report.count", "op": ">=", "value": 1}],
    )
    published = tg.publish_ready_nodes(db, graph_id)
    task_id = published[0]["task_id"]
    res = tg.evaluate_acceptance(
        db, task_id,
        result_summary={"metrics": {"report": {"count": 2}}},
    )
    assert res["accepted"] is True


# --------------------------------------------------------------------------- #
# Evidence chain (receipt-verified)
# --------------------------------------------------------------------------- #

def test_evidence_receipt_verification(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g")
    tg.add_task_node(db, graph_id, task_key="t1", goal="scan")
    published = tg.publish_ready_nodes(db, graph_id)
    task_id = published[0]["task_id"]

    eid = tg.record_task_evidence(
        db, task_id, evidence_type="http_response",
        ref="demo.test/admin", receipt_id="call-1",
        supports_metrics={"reviewer_accept": True},
    )
    tg.record_task_evidence(
        db, task_id, evidence_type="http_response",
        ref="GET /secret", receipt_id="call-2",
    )
    # no receipt -> stays pending
    tg.record_task_evidence(db, task_id, evidence_type="document", ref="note without receipt")

    receipts = [
        {"receipt_id": "call-1", "request": {"url": "https://demo.test/admin", "method": "GET"}},
        {"receipt_id": "call-2", "response": {"body": "nothing matching"}},
    ]
    result = tg.verify_evidence_receipts(db, task_id, receipts)
    assert result["verified"] == 1   # call-1 ref "demo.test/admin" in request
    assert result["rejected"] == 1   # call-2 ref "GET /secret" missing from response
    assert result["pending"] == 1    # no receipt

    rows = tg.get_task_evidence(db, task_id)
    by_status = {r["status"] for r in rows}
    assert by_status == {"verified", "rejected", "pending"}

    verified_row = next(r for r in rows if r["status"] == "verified")
    assert json.loads(verified_row["supports_metrics"]) == {"reviewer_accept": True}


# --------------------------------------------------------------------------- #
# Completion + growth (rolling admission)
# --------------------------------------------------------------------------- #

def test_complete_graph_task_updates_node_and_unlocks_children(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g")
    tg.add_task_node(
        db, graph_id, task_key="root", goal="root",
        acceptance_criteria=[{"metric": "output_nonempty", "op": "==", "value": True}],
    )
    tg.add_task_node(db, graph_id, task_key="child", goal="child", depends_on=["root"])
    published = tg.publish_ready_nodes(db, graph_id)
    root_task_id = published[0]["task_id"]

    acceptance = tg.complete_graph_task(
        db, root_task_id,
        result_summary={"metrics": {"output_nonempty": True}},
    )
    assert acceptance["accepted"] is True
    node = db.fetch_one(
        "SELECT status FROM task_graph_nodes WHERE graph_id = ? AND task_key = 'root'",
        (graph_id,),
    )
    assert node["status"] == "completed"
    g = tg.get_graph(db, graph_id)
    assert g["completed_nodes"] == 1

    published2 = tg.publish_ready_nodes(db, graph_id)
    assert len(published2) == 1
    assert published2[0]["task_key"] == "child"


def test_complete_graph_task_rejects_bad_acceptance(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g")
    tg.add_task_node(
        db, graph_id, task_key="t1", goal="t",
        acceptance_criteria=[{"metric": "verified", "op": "==", "value": True}],
    )
    published = tg.publish_ready_nodes(db, graph_id)
    task_id = published[0]["task_id"]

    acceptance = tg.complete_graph_task(db, task_id, result_summary={"metrics": {"verified": False}})
    assert acceptance["accepted"] is False
    node = db.fetch_one(
        "SELECT status FROM task_graph_nodes WHERE task_id = ?", (task_id,)
    )
    assert node["status"] == "failed"
    task = db.fetch_one("SELECT status FROM agent_tasks WHERE task_id = ?", (task_id,))
    assert task["status"] == "failed"


def test_spawn_subtask_rolling_admission(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g")
    tg.add_task_node(db, graph_id, task_key="parent", goal="parent",
                     role="scanner", task_type="scan")
    published = tg.publish_ready_nodes(db, graph_id)
    parent_task_id = published[0]["task_id"]

    # Parent completes -> spawn a subtask admitted into the same graph
    tg.complete_graph_task(db, parent_task_id, result_summary={"metrics": {}})
    spawned = tg.spawn_subtask(
        db, parent_task_id,
        task_key="child:deep", goal="deep dive",
        role="analyst", task_type="analyze",
        acceptance_criteria=[{"metric": "output_nonempty", "op": "==", "value": True}],
    )
    node = db.fetch_one(
        "SELECT * FROM task_graph_nodes WHERE node_id = ?", (spawned["node_id"],)
    )
    assert node["graph_id"] == graph_id
    assert node["run_id"] == run_id
    assert json.loads(node["depends_on"]) == ["parent"]
    # parent is completed -> child is publishable immediately
    assert any(p["task_key"] == "child:deep" for p in spawned["published"])
    g = tg.get_graph(db, graph_id)
    assert g["total_nodes"] == 2


# --------------------------------------------------------------------------- #
# Goal-level progress view
# --------------------------------------------------------------------------- #

def test_get_graph_progress(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g1", domain="web")
    tg.add_task_node(db, graph_id, task_key="a", goal="a")
    tg.add_task_node(db, graph_id, task_key="b", goal="b", depends_on=["a"])
    tg.publish_ready_nodes(db, graph_id)
    a_task = db.fetch_one("SELECT task_id FROM agent_tasks WHERE task_key = 'a'", ())
    tg.complete_graph_task(db, a_task["task_id"], result_summary={"metrics": {}})

    progress = tg.get_graph_progress(db, run_id)
    assert len(progress) == 1
    g = progress[0]
    assert g["graph_id"] == graph_id
    assert g["node_counts"]["completed"] == 1
    assert g["done_ratio"] == 0.5


def test_mark_graph_completed(db):
    run_id = make_run(db)
    graph_id = tg.create_task_graph(db, run_id, goal="g")
    tg.add_task_node(db, graph_id, task_key="a", goal="a")
    tg.publish_ready_nodes(db, graph_id)
    a_task = db.fetch_one("SELECT task_id FROM agent_tasks WHERE task_key = 'a'", ())
    tg.complete_graph_task(db, a_task["task_id"], result_summary={"metrics": {}})
    assert tg.mark_graph_completed(db, graph_id) is True
    g = tg.get_graph(db, graph_id)
    assert g["status"] == "completed"
