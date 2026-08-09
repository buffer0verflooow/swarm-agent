"""Tests for the shared signal board (src/swarm/signal_board.py)."""

import pytest

from src.swarm.signal_board import (
    publish_signal, get_signal, get_signals, attach_evidence, collect_evidence,
    get_graph_id_for_task, build_signal_context, SIGNAL_BOARD_KEY,
)
from src.swarm.task_graph import (
    create_task_graph, add_task_node, publish_ready_nodes, complete_graph_task,
)


@pytest.fixture
def db(tmp_path):
    from src.db import SwarmDB
    db = SwarmDB(str(tmp_path / "board.db"))
    db.init()
    return db


@pytest.fixture
def graph(db):
    run_id = "board-test-run"
    db.execute(
        """INSERT OR IGNORE INTO swarm_runs
           (run_id, swarm_name, intent, target_type, target_id, status)
           VALUES (?, 'board-test', 'analyze', 'webapp', 't1', 'running')""",
        (run_id,),
    )
    db.conn.commit()
    gid = create_task_graph(db, run_id, goal="Diagnose database anomaly",
                            domain="database-diagnostics", strategy="deterministic")
    return gid


def test_publish_and_read_signal(db, graph):
    publish_signal(db, graph, "probe_snapshot", {"insert_calls": 943})
    assert get_signal(db, graph, "probe_snapshot") == {"insert_calls": 943}
    assert "probe_snapshot" in get_signals(db, graph)


def test_publish_is_append_only(db, graph):
    publish_signal(db, graph, "sig", "v1")
    publish_signal(db, graph, "sig", "v2")  # no overwrite by default
    assert get_signal(db, graph, "sig") == "v1"
    publish_signal(db, graph, "sig", "v2", overwrite=True)
    assert get_signal(db, graph, "sig") == "v2"


def test_attach_and_collect_evidence(db, graph):
    attach_evidence(db, graph, "analyze:LOCK_CONTENTION",
                    {"present": True, "evidence": "20 update variants, 91k calls"})
    attach_evidence(db, graph, "analyze:VACUUM",
                    {"present": False, "evidence": "no deletes"})
    ev = collect_evidence(db, graph)
    assert set(ev.keys()) == {"analyze:LOCK_CONTENTION", "analyze:VACUUM"}
    assert ev["analyze:LOCK_CONTENTION"]["present"] is True


def test_board_persists_in_graph_metadata(db, graph):
    publish_signal(db, graph, "k", "v")
    row = db.fetch_one("SELECT metadata FROM task_graphs WHERE graph_id = ?", (graph,))
    meta = row["metadata"]
    assert isinstance(meta, str)
    import json
    assert json.loads(meta)[SIGNAL_BOARD_KEY]["k"] == "v"


def test_get_graph_id_for_task(db, graph):
    add_task_node(db, graph, "probe:stats", "collect signals", depends_on=[],
                  phase=1, priority=90, role="db-analyst")
    published = publish_ready_nodes(db, graph)
    task_id = published[0]["task_id"]
    assert get_graph_id_for_task(db, task_id) == graph
    assert get_graph_id_for_task(db, "no-such-task") is None


def test_build_signal_context_injects_board(db, graph):
    add_task_node(db, graph, "probe:stats", "collect signals", depends_on=[],
                  phase=1, priority=90, role="db-analyst",
                  acceptance_criteria=[{"metric": "n", "op": ">=", "value": 1}])
    add_task_node(db, graph, "analyze:X", "verify X", depends_on=["probe:stats"],
                  phase=2, priority=80, role="db-analyst")
    published = publish_ready_nodes(db, graph)
    probe_task = published[0]
    complete_graph_task(db, probe_task["task_id"],
                        result_summary={"metrics": {"n": 5},
                                        "result": {"snapshot": "insert 943 calls"}})

    publish_signal(db, graph, "probe_snapshot", "insert 943 calls")
    attach_evidence(db, graph, "analyze:X", {"present": True, "evidence": "found"})

    # analyze task is claimed now
    published2 = publish_ready_nodes(db, graph)
    task = db.fetch_one("SELECT * FROM agent_tasks WHERE task_id = ?",
                        (published2[0]["task_id"],))
    ctx = build_signal_context(db, dict(task))
    assert "Shared Signal Board" in ctx
    assert "insert 943 calls" in ctx
    assert "analyze:X" in ctx  # sibling evidence visible


def test_build_signal_context_noop_for_non_graph_task(db):
    db.execute(
        """INSERT OR IGNORE INTO swarm_runs
           (run_id, swarm_name, intent, target_type, target_id, status)
           VALUES ('r1', 'legacy', 'analyze', 'webapp', 'x', 'running')""",
    )
    db.conn.commit()
    db.execute(
        """INSERT INTO agent_tasks (task_id, run_id, task_type, task_intent, status)
           VALUES ('legacy-1', 'r1', 'scan', 'plain', 'pending')""",
    )
    db.conn.commit()
    task = db.fetch_one("SELECT * FROM agent_tasks WHERE task_id = ?", ("legacy-1",))
    assert build_signal_context(db, dict(task)) == ""
