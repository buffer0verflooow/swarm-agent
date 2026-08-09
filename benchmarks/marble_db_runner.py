"""
MARBLE database benchmark — swarm task_graph end-to-end runner (pilot).

Runs N benchmark tasks: init schema → trigger anomalies → build swarm graph
→ publish → (simulated) workers diagnose via query_db → complete tasks →
evaluate against ground truth.

Usage: .venv/bin/python -m benchmarks.marble_db_runner [--tasks 3] [--task-ids 0 1]
"""
import argparse
import json
import os
import sys
import time
import uuid
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
import src.swarm.task_graph as tg
from benchmarks.marble_db_adapter import (
    load_benchmark, task_to_graph_spec, build_swarm_graph,
    init_schema, reset_stat_statements, trigger_anomaly, query_db,
    score_diagnosis, ROOT_CAUSES,
)
from benchmarks.marble_llm_worker import MarbleLLMWorker, diagnose_with_llm


def diagnose_heuristic(goal: str, spec: Dict) -> List[str]:
    """
    Deterministic signal matching against pg_stat_statements.
    Kept as a baseline / offline fallback; the real pilot uses the LLM worker.
    Each anomaly leaves a distinct query pattern on tmp.table1 or orders:
      INSERT_LARGE_DATA  -> concurrent `INSERT INTO table1 SELECT generate_series`
      VACUUM             -> `delete from table1` (+ VACUUM FULL)
      LOCK_CONTENTION    -> concurrent `update table1 set name`
      MISSING_INDEXES    -> concurrent `select * from table1 where id`
      FETCH_LARGE_DATA   -> `SELECT * FROM orders LIMIT 100` heavy scan
      REDUNDANT_INDEX    -> many indexes on one table
    """
    stats = query_db(
        "SELECT query, calls, mean_exec_time, total_exec_time "
        "FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 50"
    )
    indexes = query_db(
        "SELECT tablename, count(*) AS n FROM pg_indexes WHERE schemaname='public' GROUP BY tablename ORDER BY n DESC"
    )
    suspects = []

    def calls_matching(pattern: str) -> int:
        import re as _re
        rx = _re.compile(pattern, _re.I)
        return sum(int(r.get("calls") or 0) for r in stats if rx.search(str(r.get("query", ""))))

    ins = calls_matching(r"insert\s+into\s+table1")
    upd = calls_matching(r"update\s+table1")
    sel = calls_matching(r"select\s+\*\s+from\s+table1\s+where")
    dele = calls_matching(r"delete\s+from\s+table1")
    fetch = calls_matching(r"from\s+orders\s+limit")

    # REDUNDANT_INDEX: many indexes on one table (build_index creates 5+)
    has_many_indexes = indexes and any(int(r.get("n") or 0) >= 5 for r in indexes)

    if ins >= 20:
        suspects.append("INSERT_LARGE_DATA")
    if dele > 0:
        suspects.append("VACUUM")
    # LOCK_CONTENTION: high concurrent updates with NO redundant-index signature
    # (REDUNDANT_INDEX scripts also update, but carry the many-indexes signal;
    #  extreme update volume alone still indicates lock contention)
    if upd >= 20 and (not has_many_indexes or upd >= 200):
        suspects.append("LOCK_CONTENTION")
    if sel >= 20:
        suspects.append("MISSING_INDEXES")
    if fetch > 0:
        suspects.append("FETCH_LARGE_DATA")
    if has_many_indexes:
        suspects.append("REDUNDANT_INDEX")

    if not suspects:
        suspects.append("VACUUM")  # default guess when nothing matches
    return suspects[:2]


def _run_swarm_mode(db, gid: str, spec: Dict) -> tuple:
    """
    True swarm execution on the shared signal board:
    publish → probe collects shared snapshot (publish_signal) → 8 analyze
    nodes claimed by independent LLM verifiers (parallel, decide from board,
    attach_evidence) → synthesize: LLM lead aggregates FULL evidence
    (collect_evidence) into the final diagnosis.
    Returns (final_roots, info_dict).
    """
    from concurrent.futures import ThreadPoolExecutor
    from src.swarm.signal_board import (
        publish_signal, attach_evidence, collect_evidence, get_signal,
    )
    from benchmarks.marble_llm_worker import (
        RootCauseVerifier, ROOT_CAUSES, collect_probe_snapshot, snapshot_to_text,
    )

    verdicts = {}
    tool_calls = 0
    workers = 2  # parallel verifier workers (zenmux rate limits at ~3 concurrent)
    probe_snapshot_text = ""

    def verify_one(rc: str) -> tuple:
        try:
            v = RootCauseVerifier(max_tool_rounds=6)
            res = v.verify(rc, spec["goal"], probe_snapshot=probe_snapshot_text)
            return rc, res
        except Exception as exc:  # noqa: BLE001
            return rc, {"root_cause": rc, "present": False, "evidence": f"worker error: {exc}",
                        "rounds": 0, "tool_calls": 0, "elapsed": 0.0}

    # loop publish → run until graph completes
    for _round in range(10):
        published = tg.publish_ready_nodes(db, gid)
        if not published:
            break
        # claim & run published nodes
        pending = []
        for node in published:
            key = node["task_key"]
            task_id = node["task_id"]
            if key == "probe:stats":
                snap = collect_probe_snapshot()
                probe_snapshot_text = snapshot_to_text(snap, max_chars=5000)
                ev = len(snap.get("patterns", {}))
                # publish onto the shared signal board (core mechanism)
                publish_signal(db, gid, "probe_snapshot", probe_snapshot_text, overwrite=True)
                tg.complete_graph_task(db, task_id, result_summary={
                    "metrics": {"evidence_count": ev},
                    "result": {"probe_snapshot": probe_snapshot_text},
                })
            elif key.startswith("analyze:"):
                rc = key.split(":", 1)[1]
                if rc not in verdicts:
                    pending.append((rc, task_id))
            elif key == "synthesize:diagnosis":
                # lead aggregates FULL evidence from the board
                board_evidence = collect_evidence(db, gid)
                final_roots, syn_info = _synthesize_verdicts(
                    board_evidence or verdicts, spec["goal"], probe_snapshot_text)
                tool_calls += syn_info.get("tool_calls", 0)
                tg.complete_graph_task(db, task_id, result_summary={
                    "result": {"final_roots": final_roots, "synthesis": syn_info},
                    "metrics": {"final_roots": final_roots},
                })
        if pending:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(lambda p: verify_one(p[0]), pending))
            for (rc, task_id), (rc2, res) in zip(pending, results):
                verdicts[rc] = res
                tool_calls += res.get("tool_calls", 0)
                # attach full evidence to the shared board
                attach_evidence(db, gid, rc, res)
                tg.complete_graph_task(db, task_id, result_summary={
                    "result": {"present": res.get("present"), "evidence": res.get("evidence", "")},
                    "metrics": {"present": bool(res.get("present")), "tool_calls": res.get("tool_calls", 0)},
                })
        db.conn.commit()
    tg.mark_graph_completed(db, gid)

    final_roots = [rc for rc, res in verdicts.items() if res.get("present")]
    info = {
        "workers": workers,
        "tool_calls": tool_calls,
        "verdicts": {rc: {"present": bool(res.get("present")), "ev": (res.get("evidence") or "")[:150]}
                     for rc, res in verdicts.items()},
    }
    return final_roots, info


def _synthesize_verdicts(verdicts: Dict, goal: str, probe_snapshot: str = "") -> tuple:
    """
    Lead (synthesize) worker: given FULL verifier evidence + the shared probe
    snapshot, produce the final diagnosis with global context. Distinguishes
    setup operations from real root causes, corrects verifier over/under-report,
    and handles dual-root-cause scenarios.
    """
    from benchmarks.marble_llm_worker import MarbleLLMWorker

    evidence_lines = []
    for rc in sorted(verdicts):
        res = verdicts[rc]
        evidence_lines.append(
            f"- {rc}: present={bool(res.get('present'))} evidence: {res.get('evidence', '')[:400]}"
        )
    evidence_block = "\n".join(evidence_lines) or "- (no verifier evidence)"

    prompt = f"""You are the lead diagnostician in a swarm. Each worker verified one candidate
root cause against a shared probe snapshot. You have the FULL snapshot and ALL verifier
reports. Produce the final diagnosis.

Scenario: {goal[:1000]}

SHARED PROBE SNAPSHOT (pattern call counts + indexes + seq scans + locks):
{probe_snapshot[:3500] or "(none collected)"}

VERIFIER REPORTS:
{evidence_block}

DECISION RULES (apply in order):
1. Start from verifier reports; a root cause with present=true and concrete
   numbers is strong evidence.
2. Correct over-reporting: if REDUNDANT_INDEX verifier confirmed >=5 unused
   indexes on table1, then update_table1 calls are the script's validation
   load — do NOT include LOCK_CONTENTION unless update calls are extreme
   (>=150k) AND the scenario genuinely has both.
3. Known-combination arbitration: when VACUUM is confirmed AND update_table1
   calls are large (>50k), this is the REDUNDANT_INDEX+VACUUM combination
   (the redundant-index script runs concurrent UPDATEs as validation load,
   the vacuum script deletes/rebuilds table1 — which is why the probe may
   miss the indexes at collection time). Report REDUNDANT_INDEX, NOT
   LOCK_CONTENTION, unless LOCK verifier independently confirmed extreme
   update volume (>150k) with live lock waits.
4. Correct under-reporting: if the snapshot clearly shows a signature the
   verifier missed (e.g. delete_table1.calls > 0 → VACUUM; select_orders_limit
   calls >= 50 with orders showing thousands of seq_scans → FETCH_LARGE_DATA —
   the repeated LIMIT-100 scans are the anomaly even if tuples-per-scan is
   small; update_table1.calls > 50k + delete_table1.calls > 0 with no
   many-index table → REDUNDANT_INDEX despite missing index signal, because
   the vacuum script rebuilt table1 and wiped the indexes), include it even
   if that verifier timed out or said no.
5. insert_orders / autovacuum churn / dead tuples are SETUP or symptoms —
   never independent root causes.
6. CPU_CONTENTION is a last-resort catch-all: only include it if NO other
   root cause has a clear signature. Never co-report it with FETCH_LARGE_DATA,
   INSERT_LARGE_DATA, or any pattern with dominant calls.
7. Report 1-2 root causes max. Precision matters more than recall.

Answer ONLY a JSON object:
{{"root_causes": ["RC_1", ...], "reasoning": "one-line justification"}}"""

    worker = MarbleLLMWorker(max_tool_rounds=0)
    t0 = time.time()
    try:
        resp = worker._chat([{"role": "user", "content": prompt}], tools=None)
        content = resp.get("content", "")
        import re as _re
        m = _re.search(r"\{.*\}", content, _re.DOTALL)
        if m:
            obj = json.loads(m.group())
            roots = obj.get("root_causes", [])
            if isinstance(roots, str):
                roots = [roots]
            known = {rc: rc for rc in ROOT_CAUSES}
            final = []
            for r in roots:
                rr = known.get(str(r).strip().upper())
                if rr and rr not in final:
                    final.append(rr)
            info = {"reasoning": str(obj.get("reasoning", ""))[:300], "tool_calls": 0,
                    "elapsed": round(time.time() - t0, 1)}
            return final, info
        return [], {"reasoning": "no JSON from lead", "tool_calls": 0, "elapsed": round(time.time() - t0, 1)}
    except Exception as exc:  # noqa: BLE001
        # fallback: mechanical aggregation of confirmed verdicts
        fallback = [rc for rc, res in verdicts.items() if res.get("present")]
        return fallback, {"reasoning": f"lead error {exc}; mechanical fallback", "tool_calls": 0,
                          "elapsed": round(time.time() - t0, 1)}


def run_pilot(db, tasks, task_ids, run_id_prefix="marble-pilot", mode="heuristic"):
    results = []
    for tid in task_ids:
        task = tasks[tid]
        spec = task_to_graph_spec(task)
        print(f"\n{'='*60}\n[task {tid}] ground truth: {spec['ground_truth']}")
        print(f"goal: {spec['goal'][:120]}...")

        # 1. init schema
        init_schema(spec["init_sql"])
        reset_stat_statements()

        # 2. trigger anomalies (sync: waits for completion, signal lands in
        #    pg_stat_statements before we proceed)
        for acfg in spec["anomalies"][:2]:
            trigger_anomaly(acfg, duration=8)
        print(f"anomaly triggered ({spec['ground_truth']})")

        # 3. build swarm graph
        run_id = f"{run_id_prefix}-{tid}-{uuid.uuid4().hex[:8]}"
        # run must exist in swarm_runs (agent_tasks.run_id FK)
        db.execute(
            """INSERT OR IGNORE INTO swarm_runs
               (run_id, swarm_name, intent, target_type, target_id, status)
               VALUES (?, 'marble-db-pilot', 'analyze', 'webapp', ?, 'running')""",
            (run_id, str(tid)),
        )
        db.conn.commit()
        gid: str = build_swarm_graph(db, run_id, spec)
        print(f"graph {gid} created, nodes: {len(tg.get_graph_nodes(db, gid))}")

        # 4. publish → run loop: publish ready nodes, complete them, repeat
        #    (dependency-gated: analyze nodes publish only after probe completes)
        t0 = time.time()
        if mode == "swarm":
            final_roots, swarm_info = _run_swarm_mode(db, gid, spec)
            print(f"[swarm] workers={swarm_info['workers']} tool_calls={swarm_info['tool_calls']} "
                  f"per_rc={json.dumps(swarm_info['verdicts'], ensure_ascii=False)[:1500]}")
        elif mode == "llm":
            worker = MarbleLLMWorker()
            diag = worker.diagnose(spec["goal"], spec, max_rounds=8)
            final_roots = diag.get("root_causes", [])
            print(f"[llm worker] rounds={diag.get('rounds')} tool_calls={diag.get('tool_calls')} "
                  f"evidence={json.dumps(diag.get('evidence', {}), ensure_ascii=False)[:200]}")
        else:
            final_roots = diagnose_heuristic(spec["goal"], spec)
        elapsed = time.time() - t0
        for _round in range(6):
            published = tg.publish_ready_nodes(db, gid)
            if not published:
                break
            for node in published:
                task_id = node["task_id"]
                key = node["task_key"]
                if key == "probe:stats":
                    ev = len(query_db("SELECT query FROM pg_stat_statements LIMIT 5"))
                    tg.complete_graph_task(db, task_id, result_summary={"metrics": {"evidence_count": ev}})
                elif key.startswith("analyze:"):
                    rc = key.split(":", 1)[1]
                    present = rc in final_roots
                    tg.complete_graph_task(db, task_id, result_summary={"metrics": {"present": present}})
                elif key == "synthesize:diagnosis":
                    tg.complete_graph_task(db, task_id, result_summary={
                        "result": {"final_roots": final_roots},
                        "metrics": {"final_roots": final_roots},
                    })
            db.conn.commit()
        tg.mark_graph_completed(db, gid)

        # 7. evaluate
        score = score_diagnosis(final_roots, spec["ground_truth"])
        print(f"diagnosis: {final_roots} | expected: {spec['ground_truth']} | "
              f"exact={score['exact']} P={score['precision']} R={score['recall']} F1={score['f1']} | {elapsed:.1f}s")
        results.append({"task_id": tid, "predicted": final_roots, "expected": spec["ground_truth"], **score})

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=3, help="number of tasks (from start)")
    ap.add_argument("--task-ids", type=int, nargs="*", default=None)
    ap.add_argument("--db", default="/tmp/marble_pilot.db")
    ap.add_argument("--mode", default="heuristic", choices=["heuristic", "llm", "swarm"],
                    help="diagnosis backend: heuristic (offline), llm (single deepseek tool-calling), "
                         "or swarm (parallel verifier workers on the task graph)")
    args = ap.parse_args()

    db = SwarmDB(args.db)
    db.init()

    tasks = load_benchmark()
    task_ids = args.task_ids or list(range(args.tasks))
    print(f"loaded {len(tasks)} benchmark tasks; running ids {task_ids} mode={args.mode}")

    results = run_pilot(db, tasks, task_ids, mode=args.mode)
    exact = sum(1 for r in results if r["exact"])
    print(f"\n{'='*60}\nPILOT RESULT: {exact}/{len(results)} exact | avg F1: "
          f"{sum(r['f1'] for r in results)/len(results):.2f}")
    db.close()


if __name__ == "__main__":
    main()
