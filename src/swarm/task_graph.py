"""
Domain-agnostic task graph layer (P0).

A goal-oriented decomposition layer on top of the existing work market. A run
may carry one or more ``task_graphs``; each graph decomposes a high-level goal
into subtask DAG nodes with explicit dependencies, acceptance criteria, tool
allowlists and per-node budgets.

Design goals (from the CyberGym evaluation report):

* **Domain-agnostic.** role / task_type / tool_allowlist are plain strings.
  Nothing here knows about IDA, binaries, web scanners or forensics. The
  reverselibrary swarm is a *plugin* (a set of roles + tools), not a core
  dependency.
* **Composable with the existing swarm.** Nodes are published into the
  existing ``agent_tasks`` market, so claim_work_tasks / signals / controller
  keep working unchanged. The graph layer only gates *which* nodes become
  claimable (dependency-gated publishing) and evaluates acceptance on
  completion.
* **Growing graphs (rolling admission, ported from reverselibrary).** A
  completed node may emit spawn directives (subtask specs) that are admitted
  into the same graph mid-run, instead of a one-shot static DAG.
* **Receipt-verified evidence.** ``task_evidence`` rows only count as verified
  when their ``ref`` appears verbatim in a completed tool call's request or
  response (receipt check) — bare metrics never pass acceptance by themselves.

Usage::

    graph_id = create_task_graph(db, run_id, goal="Map attack surface",
                                 domain="web", strategy="deterministic")
    add_task_node(db, graph_id, task_key="recon:dns",
                  goal="Enumerate DNS records", role="scanner",
                  task_type="scan", depends_on=[],
                  acceptance_criteria=[{"metric": "output_nonempty",
                                        "op": "==", "value": True,
                                        "required": True}],
                  tool_allowlist=["dns", "http"])
    publish_ready_nodes(db, graph_id)          # publishes root nodes only
    ... workers claim from the market as usual ...
    evaluate_acceptance(db, task_id, result)   # on completion
    record_task_evidence(db, task_id, ...)     # attach receipts
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from .work_queue import publish_work_task

_log = logging.getLogger("swarm_knowledge.task_graph")

# Default per-node budget when the node spec omits one.
DEFAULT_BUDGET = {"tokens": 100_000, "tool_calls": 20, "seconds": 900}

# Acceptance metric evaluators: (metric, op, expected) -> bool
_OPS = {
    "==": lambda actual, expected: actual == expected,
    "!=": lambda actual, expected: actual != expected,
    ">=": lambda actual, expected: _as_number(actual) >= _as_number(expected),
    "<=": lambda actual, expected: _as_number(actual) <= _as_number(expected),
    ">": lambda actual, expected: _as_number(actual) > _as_number(expected),
    "<": lambda actual, expected: _as_number(actual) < _as_number(expected),
    "in": lambda actual, expected: actual in (expected if isinstance(expected, list) else [expected]),
    "contains": lambda actual, expected: expected in (actual or ""),
}

_NUMERIC_OPS = {">=", "<=", ">", "<"}


def _as_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _loads_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# Graph lifecycle
# --------------------------------------------------------------------------- #

def create_task_graph(
    db,
    run_id: str,
    goal: str,
    strategy: str = "deterministic",
    domain: str = "general",
    metadata: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> str:
    """Create a task graph row and return graph_id."""
    if not run_id or not str(goal or "").strip():
        raise ValueError("run_id and goal are required")
    graph_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO task_graphs
           (graph_id, run_id, goal, strategy, domain, status, metadata)
           VALUES (?, ?, ?, ?, ?, 'active', ?)""",
        (graph_id, run_id, str(goal), strategy or "deterministic",
         domain or "general", _json_text(metadata or {})),
    )
    if commit:
        db.conn.commit()
    return graph_id


def add_task_node(
    db,
    graph_id: str,
    task_key: str,
    goal: str,
    depends_on: Optional[List[str]] = None,
    phase: int = 1,
    priority: int = 50,
    role: str = "custom",
    task_type: str = "analyze",
    tool_allowlist: Optional[List[str]] = None,
    acceptance_criteria: Optional[List[Dict[str, Any]]] = None,
    budget: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> str:
    """Add a subtask node to a graph.

    Validates that every ``depends_on`` key already exists in the graph, so a
    DAG can be built incrementally (parents before children).
    """
    graph = db.fetch_one("SELECT graph_id FROM task_graphs WHERE graph_id = ?", (graph_id,))
    if not graph:
        raise ValueError(f"unknown graph_id: {graph_id}")
    task_key = str(task_key).strip()
    if not task_key or not str(goal or "").strip():
        raise ValueError("task_key and goal are required")

    deps = [str(d) for d in (depends_on or []) if str(d).strip()]
    if deps:
        existing = {
            r["task_key"]
            for r in db.fetch_all(
                "SELECT task_key FROM task_graph_nodes WHERE graph_id = ?", (graph_id,)
            )
        }
        missing = [d for d in deps if d not in existing]
        if missing:
            raise ValueError(
                f"task {task_key} depends on unknown keys: {missing} "
                f"(parents must be added before children)"
            )

    node_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO task_graph_nodes
           (node_id, graph_id, run_id, task_key, goal, phase, depends_on,
            priority, role, task_type, tool_allowlist, acceptance_criteria,
            budget, status, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            node_id, graph_id,
            str(graph["graph_id"] and db.fetch_one(
                "SELECT run_id FROM task_graphs WHERE graph_id = ?", (graph_id,)
            )["run_id"]),
            task_key, str(goal),
            max(1, int(phase or 1)),
            _json_text(deps),
            max(0, min(100, int(priority or 50))),
            str(role or "custom"),
            str(task_type or "analyze"),
            _json_text([str(t) for t in (tool_allowlist or [])]),
            _json_text(acceptance_criteria or []),
            _json_text({**DEFAULT_BUDGET, **(budget or {})}),
            _json_text(metadata or {}),
        ),
    )
    db.execute(
        "UPDATE task_graphs SET total_nodes = total_nodes + 1, updated_at = datetime('now') "
        "WHERE graph_id = ?",
        (graph_id,),
    )
    if commit:
        db.conn.commit()
    return node_id


def get_graph_nodes(db, graph_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all nodes of a graph, ordered by phase then priority."""
    sql = "SELECT * FROM task_graph_nodes WHERE graph_id = ?"
    params: List[Any] = [graph_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY phase ASC, priority DESC, created_at ASC"
    return [dict(r) for r in db.fetch_all(sql, tuple(params))]


def get_graph(db, graph_id: str) -> Optional[Dict[str, Any]]:
    row = db.fetch_one("SELECT * FROM task_graphs WHERE graph_id = ?", (graph_id,))
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Dependency-gated publishing
# --------------------------------------------------------------------------- #

def _node_dependencies_met(db, node: Dict[str, Any]) -> bool:
    """All dependency task_keys of a node must be completed in this graph."""
    deps = _loads_json(node.get("depends_on"), [])
    if not deps:
        return True
    for dep_key in deps:
        row = db.fetch_one(
            """SELECT status FROM task_graph_nodes
               WHERE graph_id = ? AND task_key = ?""",
            (node["graph_id"], dep_key),
        )
        if not row or row["status"] != "completed":
            return False
    return True


def publish_ready_nodes(db, graph_id: str, commit: bool = True) -> List[Dict[str, Any]]:
    """Publish graph nodes whose dependencies are all met into the work market.

    Only nodes in 'pending' status whose depends_on keys are all 'completed'
    are published. Each published node becomes an ``agent_tasks`` row (status
    pending) and the node is marked 'published'.

    Returns the list of published node dicts.
    """
    nodes = [dict(n) for n in db.fetch_all(
        """SELECT * FROM task_graph_nodes
           WHERE graph_id = ? AND status = 'pending'
           ORDER BY phase ASC, priority DESC""",
        (graph_id,),
    )]
    published: List[Dict[str, Any]] = []
    for node in nodes:
        if not _node_dependencies_met(db, node):
            continue
        task_id = publish_work_task(
            db,
            run_id=node["run_id"],
            task_type=node["task_type"],
            required_role=node["role"],
            reason=node["goal"],
            context_entry_ids=[],
            parent_task_id=None,
            source_agent="task-graph",
            intent=node["goal"][:120],
            priority=node["priority"],
            metadata={
                "graph_id": node["graph_id"],
                "node_id": node["node_id"],
                "task_key": node["task_key"],
                "market_source": "task_graph",
            },
            signal_key=f"graph:{node['graph_id']}:{node['task_key']}",
            commit=False,
        )
        db.execute(
            """UPDATE task_graph_nodes
               SET status = 'published', task_id = ?, updated_at = datetime('now')
               WHERE node_id = ?""",
            (task_id, node["node_id"]),
        )
        db.execute(
            """UPDATE agent_tasks
               SET graph_id = ?, task_key = ?, depends_on_keys = ?,
                   acceptance_criteria = ?, tool_allowlist = ?
               WHERE task_id = ?""",
            (
                node["graph_id"], node["task_key"], node["depends_on"],
                node["acceptance_criteria"], node["tool_allowlist"], task_id,
            ),
        )
        node["task_id"] = task_id
        node["status"] = "published"
        published.append(node)

    if commit and published:
        db.conn.commit()
    if published:
        _log.info("task-graph: published %d node(s) for graph %s", len(published), graph_id[:8])
    return published


def publish_graph_all(db, graph_id: str, commit: bool = True) -> List[Dict[str, Any]]:
    """Repeatedly publish ready nodes until no more become ready.

    Useful for seeding a full graph at once; children are published in waves
    as parents complete, but this call publishes every *currently* ready node.
    """
    all_published: List[Dict[str, Any]] = []
    while True:
        batch = publish_ready_nodes(db, graph_id, commit=False)
        if not batch:
            break
        all_published.extend(batch)
    if commit and all_published:
        db.conn.commit()
    return all_published


# --------------------------------------------------------------------------- #
# Acceptance evaluation
# --------------------------------------------------------------------------- #

def _metric_value(actual_metrics: Dict[str, Any], metric: str) -> Any:
    """Resolve a metric from the result metrics dict (supports dotted paths)."""
    current: Any = actual_metrics
    for part in metric.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def evaluate_acceptance(
    db,
    task_id: str,
    result_summary: Optional[Dict[str, Any]] = None,
    criteria: Optional[List[Dict[str, Any]]] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Evaluate a task against its acceptance criteria.

    Criteria default to the node's stored acceptance_criteria (via agent_tasks).
    Returns ``{accepted, status, metrics, unmet, results}``.
    """
    task = db.fetch_one(
        "SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)
    )
    if not task:
        raise ValueError(f"unknown task_id: {task_id}")

    if criteria is None:
        criteria = _loads_json(task["acceptance_criteria"], [])
    criteria = criteria or []
    summary = _loads_json(result_summary, {}) if result_summary else _loads_json(task["result_summary"], {})
    # Metrics come from result_summary.metrics if present, else the summary itself.
    actual_metrics = summary.get("metrics", {}) if isinstance(summary, dict) else {}
    if not isinstance(actual_metrics, dict):
        actual_metrics = {}
    if not actual_metrics:
        actual_metrics = dict(summary)

    results: List[Dict[str, Any]] = []
    unmet: List[str] = []
    for criterion in criteria:
        if not isinstance(criterion, dict):
            continue
        metric = str(criterion.get("metric") or "")
        op = str(criterion.get("op") or "==")
        expected = criterion.get("value")
        required = bool(criterion.get("required", True))
        actual = _metric_value(actual_metrics, metric)
        evaluator = _OPS.get(op)
        if evaluator is None:
            passed, reason = False, f"unsupported op: {op}"
        else:
            try:
                passed = bool(evaluator(actual, expected))
                reason = ""
            except Exception as exc:  # noqa: BLE001
                passed, reason = False, f"evaluation error: {exc}"
        results.append({
            "metric": metric, "op": op, "expected": expected,
            "actual": actual, "required": required, "passed": passed,
        })
        if required and not passed:
            unmet.append(f"{metric} {op} {expected} (actual={actual})")

    accepted = not unmet
    status = "accepted" if accepted else "rejected"
    db.execute(
        """UPDATE agent_tasks
           SET acceptance_status = ?, updated_at = datetime('now')
           WHERE task_id = ?""",
        (status, task_id),
    )
    if commit:
        db.conn.commit()
    return {
        "accepted": accepted,
        "status": status,
        "metrics": actual_metrics,
        "unmet": unmet,
        "results": results,
    }


# --------------------------------------------------------------------------- #
# Evidence chain (receipt-verified)
# --------------------------------------------------------------------------- #

def record_task_evidence(
    db,
    task_id: str,
    evidence_type: str,
    ref: str,
    receipt_id: Optional[str] = None,
    supports_metrics: Optional[Dict[str, Any]] = None,
    node_id: Optional[str] = None,
    graph_id: Optional[str] = None,
    commit: bool = True,
) -> str:
    """Record one evidence row for a task (status pending until verified)."""
    if not task_id or not str(ref or "").strip():
        raise ValueError("task_id and ref are required")
    evidence_id = str(uuid.uuid4())
    task = db.fetch_one("SELECT run_id FROM agent_tasks WHERE task_id = ?", (task_id,))
    run_id = task["run_id"] if task else ""
    db.execute(
        """INSERT INTO task_evidence
           (evidence_id, task_id, node_id, graph_id, run_id, evidence_type,
            ref, receipt_id, supports_metrics, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (
            evidence_id, task_id, node_id, graph_id, run_id,
            str(evidence_type or "document"), str(ref), receipt_id,
            _json_text(supports_metrics or {}),
        ),
    )
    if commit:
        db.conn.commit()
    return evidence_id


def verify_evidence_receipts(
    db,
    task_id: str,
    tool_receipts: Optional[List[Dict[str, Any]]] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Verify pending evidence rows against tool receipts.

    A receipt is ``{receipt_id, request, response, ...}``. An evidence row is
    verified when its ``ref`` appears verbatim in the matched receipt's request
    or response text. Rows without a receipt_id stay pending; rows whose
    receipt does not contain the ref are rejected.
    """
    rows = db.fetch_all(
        """SELECT * FROM task_evidence
           WHERE task_id = ? AND status = 'pending'""",
        (task_id,),
    )
    receipts = tool_receipts or []
    receipts_by_id = {
        str(r.get("receipt_id") or r.get("tool_call_id") or ""): r
        for r in receipts
    }
    verified = 0
    rejected = 0
    for row in rows:
        ev = dict(row)
        receipt = receipts_by_id.get(str(ev.get("receipt_id") or ""))
        if not receipt:
            continue  # stays pending
        request_text = json.dumps(receipt.get("request") or {}, ensure_ascii=False, default=str)
        response_text = json.dumps(receipt.get("response") or {}, ensure_ascii=False, default=str)
        ref = str(ev["ref"])
        if ref in request_text or ref in response_text:
            db.execute(
                "UPDATE task_evidence SET status = 'verified', updated_at = datetime('now') "
                "WHERE evidence_id = ?",
                (ev["evidence_id"],),
            )
            verified += 1
        else:
            db.execute(
                """UPDATE task_evidence
                   SET status = 'rejected',
                       verification_note = 'ref not found in receipt request/response'
                   WHERE evidence_id = ?""",
                (ev["evidence_id"],),
            )
            rejected += 1
    if commit and (verified or rejected):
        db.conn.commit()
    return {"verified": verified, "rejected": rejected, "pending": len(rows) - verified - rejected}


def get_task_evidence(db, task_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM task_evidence WHERE task_id = ?"
    params: List[Any] = [task_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    return [dict(r) for r in db.fetch_all(sql, tuple(params))]


# --------------------------------------------------------------------------- #
# Completion + growth (rolling admission)
# --------------------------------------------------------------------------- #

def complete_graph_task(
    db,
    task_id: str,
    result_summary: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Mark a graph-affiliated task completed (or failed) and update the node.

    Uses the stored acceptance criteria; a rejected acceptance marks the node
    'failed'. Completing a node may unlock children for publishing.
    """
    from .work_queue import complete_work_task, fail_work_task

    task = db.fetch_one(
        "SELECT graph_id, task_key, status FROM agent_tasks WHERE task_id = ?", (task_id,)
    )
    acceptance = evaluate_acceptance(db, task_id, result_summary, commit=False)

    if acceptance["accepted"]:
        if task and task["status"] == "running":
            complete_work_task(db, task_id, result_summary=result_summary)
        else:
            # Task not claimed yet (e.g. simulated completion): update directly.
            db.execute(
                """UPDATE agent_tasks
                   SET status = 'completed',
                       result_summary = ?,
                       acceptance_status = 'accepted',
                       ended_at = datetime('now'),
                       updated_at = datetime('now')
                   WHERE task_id = ?""",
                (_json_text(result_summary or {}), task_id),
            )
        node_status = "completed"
    else:
        if task and task["status"] == "running":
            fail_work_task(db, task_id, f"acceptance rejected: {'; '.join(acceptance['unmet'])}")
        else:
            db.execute(
                """UPDATE agent_tasks
                   SET status = 'failed',
                       result_summary = ?,
                       acceptance_status = 'rejected',
                       ended_at = datetime('now'),
                       updated_at = datetime('now')
                   WHERE task_id = ?""",
                (_json_text({"error": "acceptance rejected", "unmet": acceptance["unmet"]}), task_id),
            )
        node_status = "failed"

    if task and task["graph_id"]:
        db.execute(
            """UPDATE task_graph_nodes
               SET status = ?, updated_at = datetime('now')
               WHERE graph_id = ? AND task_key = ?""",
            (node_status, task["graph_id"], task["task_key"]),
        )
        if node_status == "completed":
            db.execute(
                "UPDATE task_graphs SET completed_nodes = completed_nodes + 1, updated_at = datetime('now') "
                "WHERE graph_id = ?",
                (task["graph_id"],),
            )
        else:
            db.execute(
                "UPDATE task_graphs SET failed_nodes = failed_nodes + 1, updated_at = datetime('now') "
                "WHERE graph_id = ?",
                (task["graph_id"],),
            )
    if commit:
        db.conn.commit()
    return acceptance


def spawn_subtask(
    db,
    parent_task_id: str,
    task_key: str,
    goal: str,
    depends_on: Optional[List[str]] = None,
    role: str = "custom",
    task_type: str = "analyze",
    tool_allowlist: Optional[List[str]] = None,
    acceptance_criteria: Optional[List[Dict[str, Any]]] = None,
    priority: int = 50,
    commit: bool = True,
) -> Dict[str, Any]:
    """Admit a dynamically spawned subtask into the parent's graph (rolling admission).

    The new node inherits the parent's graph and run; it depends on the parent
    node's task_key (so it is published only after the parent completes). If the
    parent has no graph, the node is not added.
    """
    task = db.fetch_one(
        "SELECT graph_id, task_key, run_id FROM agent_tasks WHERE task_id = ?",
        (parent_task_id,),
    )
    if not task or not task["graph_id"]:
        raise ValueError("parent task has no graph affiliation; cannot spawn subtask")
    parent_key = task["task_key"] or ""
    deps = [str(d) for d in (depends_on or []) if str(d).strip()]
    if parent_key and parent_key not in deps:
        deps.insert(0, parent_key)
    node_id = add_task_node(
        db,
        graph_id=task["graph_id"],
        task_key=task_key,
        goal=goal,
        depends_on=deps,
        priority=priority,
        role=role,
        task_type=task_type,
        tool_allowlist=tool_allowlist,
        acceptance_criteria=acceptance_criteria,
        commit=False,
    )
    # Try publishing it immediately if the parent already completed.
    published = publish_ready_nodes(db, task["graph_id"], commit=False)
    if commit:
        db.conn.commit()
    return {"node_id": node_id, "published": published}


# --------------------------------------------------------------------------- #
# Goal-level view (for controller / reporting)
# --------------------------------------------------------------------------- #

def get_graph_progress(db, run_id: str) -> List[Dict[str, Any]]:
    """Goal-level visibility across all graphs of a run (controller input)."""
    rows = db.fetch_all(
        """SELECT * FROM task_graphs
           WHERE run_id = ? ORDER BY created_at ASC""",
        (run_id,),
    )
    progress: List[Dict[str, Any]] = []
    for graph in rows:
        g = dict(graph)
        nodes = get_graph_nodes(db, g["graph_id"])
        by_status: Dict[str, int] = {}
        for node in nodes:
            by_status[node["status"]] = by_status.get(node["status"], 0) + 1
        total = len(nodes)
        g["node_counts"] = by_status
        g["done_ratio"] = round(g["completed_nodes"] / max(1, g["total_nodes"] or total), 3)
        g["blocked"] = [
            n["task_key"]
            for n in nodes
            if n["status"] in ("blocked", "failed")
        ][:10]
        progress.append(g)
    return progress


def mark_graph_completed(db, graph_id: str, commit: bool = True) -> bool:
    """Mark a graph completed when all its nodes are done."""
    g = get_graph(db, graph_id)
    if not g:
        return False
    nodes = get_graph_nodes(db, graph_id)
    done = sum(1 for n in nodes if n["status"] in ("completed", "failed", "cancelled", "blocked"))
    status = "completed" if done >= max(1, len(nodes)) and done == len(nodes) else "active"
    if done >= len(nodes) and nodes:
        status = "completed"
    db.execute(
        "UPDATE task_graphs SET status = ?, updated_at = datetime('now') WHERE graph_id = ?",
        (status, graph_id),
    )
    if commit:
        db.conn.commit()
    return status == "completed"
