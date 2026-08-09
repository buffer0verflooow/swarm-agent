"""End-to-end demo: a cross-domain task graph run through the full swarm loop.

Simulates a CyberGym-style task spanning two domains (web + network) to prove
the task graph layer is domain-agnostic: decompose goal -> publish dependency-
gated nodes -> claim via market -> complete with acceptance + evidence ->
spawn subtask (rolling admission) -> graph completes.

Run: .venv/bin/python -m tests.demo_task_graph_loop
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from src.swarm import task_graph as tg
from src.swarm.lifecycle import AgentLifecycle
from src.swarm.work_queue import claim_work_tasks
from src.swarm.worker import SwarmWorker, WorkerResult


def demo() -> None:
    tmp = tempfile.mkdtemp(prefix="tg-demo-")
    db_path = os.path.join(tmp, "demo.db")
    db = SwarmDB(db_path)
    db.init()

    run_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO swarm_runs
           (run_id, swarm_name, intent, target_type, target_id, status)
           VALUES (?, 'cybergym-demo', 'custom', 'webapp', 'demo.test', 'running')""",
        (run_id,),
    )
    db.conn.commit()

    # ── 1. 目标拆解：跨域任务图（web + network，无任何逆向绑定） ──
    graph_id = tg.create_task_graph(
        db, run_id,
        goal="Assess demo.test: web entry + network exposure",
        strategy="deterministic", domain="cross",
    )
    tg.add_task_node(db, graph_id, task_key="web:recon", goal="Enumerate web endpoints",
                     role="scanner", task_type="scan", priority=90,
                     tool_allowlist=["http", "curl"],
                     acceptance_criteria=[{"metric": "endpoints", "op": ">=", "value": 1}])
    tg.add_task_node(db, graph_id, task_key="net:ports", goal="Port scan the host",
                     role="scanner", task_type="scan", priority=85,
                     tool_allowlist=["nmap"],
                     acceptance_criteria=[{"metric": "open_ports", "op": ">=", "value": 1}])
    tg.add_task_node(db, graph_id, task_key="analysis:correlate",
                     goal="Correlate web + network findings", role="analyst",
                     task_type="analyze", depends_on=["web:recon", "net:ports"],
                     priority=70,
                     acceptance_criteria=[{"metric": "correlation_count", "op": ">=", "value": 1}])
    tg.add_task_node(db, graph_id, task_key="report:summary",
                     goal="Produce final summary", role="reporter",
                     task_type="report", depends_on=["analysis:correlate"],
                     priority=50,
                     acceptance_criteria=[{"metric": "output_nonempty", "op": "==", "value": True}])

    # ── 2. 依赖门控发布：只有两个根节点可领取 ──
    published = tg.publish_ready_nodes(db, graph_id)
    keys = sorted(p["task_key"] for p in published)
    assert keys == ["net:ports", "web:recon"], f"expected roots only, got {keys}"
    print("P0 publish (roots only):", keys)

    # ── 3. Worker 从市场领取并执行（两个域各一个 scanner） ──
    def make_executor(result_metrics):
        async def executor(task, context):
            return {"success": True, "content": f"done {task['task_key']}",
                    "metrics": result_metrics}
        return executor

    results = {}
    for agent_id, role, task_key, metrics in [
        ("scanner-web", "scanner", "web:recon", {"endpoints": 5}),
        ("scanner-net", "scanner", "net:ports", {"open_ports": 3}),
    ]:
        worker = SwarmWorker(db, run_id, agent_id, role,
                             executor=make_executor(metrics), poll_interval=0.1)
        worker.register()
        import asyncio
        result: WorkerResult = asyncio.run(worker.run_once())
        assert result and result.status == "completed", f"{agent_id} failed: {result}"
        claimed_task = db.fetch_one("SELECT task_key FROM agent_tasks WHERE task_id = ?",
                                    (result.task_id,))
        assert claimed_task["task_key"] == task_key, f"{agent_id} got {claimed_task['task_key']}"
        tg.complete_graph_task(db, result.task_id, result_summary={"metrics": metrics})
        results[task_key] = "completed"
    print("P1 web+network scan completed:", results)

    # ── 4. 依赖解锁：correlate 现在可发布 ──
    published2 = tg.publish_ready_nodes(db, graph_id)
    assert [p["task_key"] for p in published2] == ["analysis:correlate"], published2
    print("P2 dependency unlock:", [p["task_key"] for p in published2])

    # ── 5. 验收 + 证据链（receipt-verified） ──
    correlate_task = published2[0]["task_id"]
    tg.record_task_evidence(db, correlate_task, evidence_type="command_output",
                            ref="nmap demo.test", receipt_id="rc-1")
    tg.record_task_evidence(db, correlate_task, evidence_type="http_response",
                            ref="GET /api", receipt_id="rc-2")
    tg.verify_evidence_receipts(db, correlate_task, [
        {"receipt_id": "rc-1", "request": {"cmd": "nmap demo.test -p 80,443"}},
        {"receipt_id": "rc-2", "response": {"body": "GET /api -> 200"}},
    ])
    acc = tg.complete_graph_task(db, correlate_task,
                                 result_summary={"metrics": {"correlation_count": 2}})
    assert acc["accepted"]
    ev = tg.get_task_evidence(db, correlate_task)
    assert {e["status"] for e in ev} == {"verified"}
    print("P3 correlate accepted; evidence verified:", len(ev))

    # ── 6. 滚动接力：analyst 派生深挖子任务，图生长 ──
    spawned = tg.spawn_subtask(db, correlate_task, task_key="analysis:deep",
                               goal="Deep-dive correlated finding", role="analyst",
                               task_type="analyze",
                               acceptance_criteria=[{"metric": "output_nonempty", "op": "==", "value": True}])
    assert any(p["task_key"] == "analysis:deep" for p in spawned["published"])
    deep_task = db.fetch_one(
        "SELECT task_id FROM agent_tasks WHERE task_key = 'analysis:deep'", ())
    tg.complete_graph_task(db, deep_task["task_id"], result_summary={"metrics": {"output_nonempty": True}})
    print("P4 rolling admission: spawned analysis:deep, completed")

    # ── 7. 报告节点 + 图完成（report 已随 spawn 一同发布，依赖满足） ──
    published3 = tg.publish_ready_nodes(db, graph_id)
    report_keys = [p["task_key"] for p in published3] + [
        p["task_key"] for p in spawned["published"]
    ]
    assert "report:summary" in report_keys, (published3, spawned["published"])
    report_row = db.fetch_one(
        "SELECT task_id FROM agent_tasks WHERE task_key = 'report:summary'", ())
    assert report_row, "report:summary should have been published"
    tg.complete_graph_task(db, report_row["task_id"],
                           result_summary={"metrics": {"output_nonempty": True}})
    assert tg.mark_graph_completed(db, graph_id)

    progress = tg.get_graph_progress(db, run_id)
    g = progress[0]
    print(f"P5 graph completed: total={g['total_nodes']} "
          f"completed={g['node_counts'].get('completed')} "
          f"status={g['status']}")
    assert g["status"] == "completed"
    assert g["node_counts"]["completed"] == g["total_nodes"]

    print("\nDEMO PASSED — cross-domain task graph ran the full swarm loop")
    db.close()


if __name__ == "__main__":
    demo()


def test_demo_task_graph_loop():
    """Run the cross-domain end-to-end demo as a pytest test."""
    demo()
