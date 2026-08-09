"""
MARBLE database benchmark → swarm-knowledge task_graph adapter.

Turns MultiAgentBench database tasks (database_main.jsonl) into swarm
task graphs. Each benchmark task becomes one goal-level task with
per-root-cause sub-tasks; workers diagnose via query_db against the
live PostgreSQL (pg_stat_statements / pg_stat_database), and the
evaluator scores output against ground-truth anomaly labels.

Environment requirements (started by setup):
  - docker compose stack in MARBLE/marble/environments/db_env_docker
    (postgres on :5432, prometheus on :9091, node_exporter, pg_exporter)
  - pg_stat_statements extension created
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import psycopg2

DB_ARGS: Dict[str, Any] = dict(user="test", password="Test123_456", host="localhost", port=5432, dbname="sysbench")

ROOT_CAUSES = [
    "INSERT_LARGE_DATA", "MISSING_INDEXES", "LOCK_CONTENTION", "VACUUM",
    "REDUNDANT_INDEX", "FETCH_LARGE_DATA", "POOR_JOIN_PERFORMANCE", "CPU_CONTENTION",
]

MARBLE_DIR = "/home/pwn/workspace/research/MARBLE"
DB_ENV_DIR = os.path.join(MARBLE_DIR, "marble/environments/db_env_docker")


# ─────────────────────────────────────────────────────────────
# 1. Database environment helpers
# ─────────────────────────────────────────────────────────────

def _connect() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(**DB_ARGS)
    conn.autocommit = True
    return conn


def exec_sql(sql: str, fetch: bool = False) -> Any:
    """Execute SQL on the sysbench database (autocommit)."""
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        if fetch:
            return cur.fetchall()
        return None
    finally:
        cur.close()
        conn.close()


def query_db(sql: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Safe diagnostic query returning rows as dicts (used by workers)."""
    sql = sql.strip().rstrip(";")
    # guard: read-only-ish, block destructive statements
    low = sql.lower()
    for bad in ("delete", "drop ", "truncate", "alter ", "update ", "create "):
        if bad in low:
            return [{"error": f"blocked statement: {bad.strip()}"}]
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM ({sql}) AS __q LIMIT {int(limit)}")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]


def init_schema(init_sql: str) -> None:
    """Apply benchmark init_sql (create tables + sample data)."""
    # drop public tables first so repeated runs are idempotent
    existing = exec_sql(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'", fetch=True
    ) or []
    for (tbl,) in existing:
        exec_sql(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')
    for stmt in split_sql(init_sql):
        if stmt.strip():
            exec_sql(stmt)


def split_sql(sql: str) -> List[str]:
    """Split a SQL script on ';' outside quotes/comments."""
    out, buf, in_str, in_comment = [], "", None, False
    for ch in sql:
        if in_comment:
            if ch == "\n":
                in_comment = False
            continue
        if in_str:
            buf += ch
            if ch == in_str:
                in_str = None
            continue
        if ch == "'" or ch == '"':
            in_str = ch
            buf += ch
        elif ch == "-" and buf.endswith("-"):
            buf = buf[:-1]
            in_comment = True
        elif ch == ";":
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return [s for s in out if s.strip()]


def reset_stat_statements() -> None:
    exec_sql("SELECT pg_stat_statements_reset();")


def wait_for_signal(signal_sql: str, timeout: float = 60.0, poll: float = 2.0) -> bool:
    """Poll pg_stat_statements until the signal query matches (anomaly visible)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rows = query_db(signal_sql)
            if rows and rows[0].get("n", 0) > 0:
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def trigger_anomaly(anomaly_cfg: Dict[str, Any], duration: int = 20) -> None:
    """
    Trigger a single anomaly against the tmp database using MARBLE's
    anomaly_trigger tooling, SYNCHRONOUSLY (waits for completion so
    concurrent tasks never contend on the tmp database).

    anomaly_cfg: {anomaly, threads, ncolumn, nrow, colsize}.
    """
    kind = anomaly_cfg["anomaly"]
    threads = min(int(anomaly_cfg.get("threads", 50)), 20)  # cap concurrency
    ncol = anomaly_cfg.get("ncolumn", 10)
    nrow = min(int(anomaly_cfg.get("nrow", 5000)), 5000)
    colsize = anomaly_cfg.get("colsize", 100)
    at_dir = os.path.join(DB_ENV_DIR, "anomaly_trigger")
    venv_py = "/home/pwn/workspace/research/swarm-knowledge/.venv/bin/python"
    cmd = (
        f"cd {at_dir} && {venv_py} main.py --anomaly {kind} "
        f"--threads {threads} --duration {duration} "
        f"--ncolumn {ncol} --nrow {nrow} --colsize {colsize}"
    )
    import subprocess
    try:
        subprocess.run(cmd, shell=True, timeout=duration + 30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        # kill leftover process group if the script overruns
        subprocess.run(f"pkill -f 'main.py --anomaly {kind}'", shell=True, stdout=subprocess.DEVNULL)


# ─────────────────────────────────────────────────────────────
# 2. Benchmark task loading
# ─────────────────────────────────────────────────────────────

def load_benchmark(path: Optional[str] = None) -> List[Dict[str, Any]]:
    path = path or os.path.join(MARBLE_DIR, "multiagentbench/database/database_main.jsonl")
    tasks = []
    with open(path) as f:
        for line in f:
            if line.strip():
                tasks.append(json.loads(line))
    return tasks


def task_to_graph_spec(task: Dict[str, Any]) -> Dict[str, Any]:
    """Map a MARBLE database task to a swarm task-graph spec."""
    env = task.get("environment", {})
    anomalies = env.get("anomalies", [])
    ground_truth = [a["anomaly"] for a in anomalies]
    content = task.get("task", {}).get("content", "")
    return {
        "task_id": task.get("task_id"),
        "goal": content,
        "init_sql": env.get("init_sql", ""),
        "anomalies": anomalies,
        "ground_truth": ground_truth,
        "max_iterations": env.get("max_iterations", 5),
    }


# ─────────────────────────────────────────────────────────────
# 3. Swarm integration (task_graph)
# ─────────────────────────────────────────────────────────────

def build_swarm_graph(db, run_id: str, spec: Dict[str, Any]) -> str:
    """Create task graph: 1 goal + per-root-cause diagnosis sub-tasks.
    Returns graph_id."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import src.swarm.task_graph as tg  # noqa: E402

    goal = spec["goal"][:1500]
    gid: str = tg.create_task_graph(
        db, run_id, goal=goal, strategy="deterministic",
        domain="database-diagnostics", metadata={"benchmark": "marble-db", "task_id": spec["task_id"]},
    )

    # phase 1: probe + signal collection
    tg.add_task_node(db, gid, "probe:stats", "Query pg_stat_statements / pg_stat_database for anomaly signals",
                     depends_on=[], phase=1, priority=90, role="db-analyst",
                     acceptance_criteria=[
                         {"metric": "evidence_count", "op": ">=", "value": 1},
                     ])
    # phase 2: per-root-cause analysis (parallel, depends on probe)
    for i, rc in enumerate(ROOT_CAUSES):
        tg.add_task_node(db, gid, f"analyze:{rc}", f"Assess whether root cause '{rc}' is present",
                         depends_on=["probe:stats"], phase=2, priority=80 - i, role="db-analyst",
                         acceptance_criteria=[
                             {"metric": "present", "op": "in", "value": [True, False]},
                         ])
    # phase 3: synthesis → final root causes
    tg.add_task_node(db, gid, "synthesize:diagnosis",
                     "Combine per-cause analyses into final diagnosis list",
                     depends_on=[f"analyze:{rc}" for rc in ROOT_CAUSES], phase=3, priority=90,
                     role="db-lead",
                     acceptance_criteria=[
                         {"metric": "final_roots", "op": "!=", "value": None},
                     ])
    return gid


# ─────────────────────────────────────────────────────────────
# 4. Evaluation
# ─────────────────────────────────────────────────────────────

def score_diagnosis(predicted: List[str], ground_truth: List[str]) -> Dict[str, Any]:
    """Accuracy: exact set match; partial: overlap ratio."""
    pred_set = set(predicted)
    gt_set = set(ground_truth)
    exact = pred_set == gt_set
    if not gt_set:
        return {"exact": exact, "precision": 1.0 if not pred_set else 0.0,
                "recall": 1.0, "f1": 1.0 if not pred_set else 0.0}
    tp = len(pred_set & gt_set)
    precision = tp / len(pred_set) if pred_set else 0.0
    recall = tp / len(gt_set)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"exact": exact, "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}
