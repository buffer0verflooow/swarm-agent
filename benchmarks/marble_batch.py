"""
Full-scale MARBLE batch runner with checkpoint/resume.

Runs all 100 database tasks in the given mode, persisting per-task results
to a JSON checkpoint file so an interrupted run can resume without redoing
completed tasks. Produces a summary table at the end.

Usage:
    .venv/bin/python -m benchmarks.marble_batch --mode swarm --start 0 --end 100
    .venv/bin/python -m benchmarks.marble_batch --mode heuristic --start 0 --end 100
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from benchmarks.marble_db_adapter import load_benchmark, task_to_graph_spec
from benchmarks.marble_db_runner import run_pilot

CHECKPOINT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "marble_batch_results.json")


def load_checkpoint() -> Dict[int, Dict]:
    if os.path.exists(CHECKPOINT_PATH):
        try:
            data = json.load(open(CHECKPOINT_PATH))
            return {int(k): v for k, v in data.items()}
        except Exception:
            return {}
    return {}


def save_checkpoint(results: Dict[int, Dict]) -> None:
    tmp = CHECKPOINT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({str(k): v for k, v in sorted(results.items())}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CHECKPOINT_PATH)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="swarm", choices=["heuristic", "llm", "swarm"])
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=100)
    ap.add_argument("--db", default="/tmp/marble_batch.db")
    ap.add_argument("--checkpoint", default=None,
                    help="checkpoint path (default: benchmarks/marble_batch_results.json)")
    ap.add_argument("--force", action="store_true", help="ignore checkpoint and redo")
    args = ap.parse_args()

    global CHECKPOINT_PATH
    if args.checkpoint:
        CHECKPOINT_PATH = args.checkpoint

    db = SwarmDB(args.db)
    db.init()

    tasks = load_benchmark()
    ids = list(range(args.start, min(args.end, len(tasks))))

    checkpoint = {} if args.force else load_checkpoint()
    # keep only entries for this run's mode
    checkpoint = {tid: r for tid, r in checkpoint.items()
                  if r.get("mode") == args.mode and tid in ids}
    todo = [tid for tid in ids if tid not in checkpoint]
    print(f"[batch] mode={args.mode} total={len(ids)} done={len(ids) - len(todo)} todo={len(todo)} "
          f"start={args.start} end={min(args.end, len(tasks))}", flush=True)

    t_start = time.time()
    for idx, tid in enumerate(todo, 1):
        t0 = time.time()
        try:
            results = run_pilot(db, tasks, [tid], mode=args.mode)
            r = results[0]
            r["mode"] = args.mode
            checkpoint[tid] = r
            print(f"[batch] {idx}/{len(todo)} task {tid} exact={r['exact']} F1={r['f1']:.2f} "
                  f"pred={r['predicted']} exp={r['expected']} ({time.time()-t0:.0f}s)", flush=True)
        except Exception as exc:  # noqa: BLE001
            checkpoint[tid] = {"task_id": tid, "mode": args.mode, "exact": False, "f1": 0.0,
                               "predicted": [], "expected": [], "error": str(exc)[:200]}
            print(f"[batch] task {tid} ERROR: {str(exc)[:150]}", flush=True)
        save_checkpoint(checkpoint)

    elapsed = time.time() - t_start
    done = [r for tid, r in checkpoint.items() if tid in ids]
    exact = sum(1 for r in done if r.get("exact"))
    f1s = [r.get("f1", 0.0) for r in done]
    avg_f1 = sum(f1s) / len(f1s) if f1s else 0.0
    # per-root-cause breakdown
    from collections import Counter
    rc_stats = {}
    for r in done:
        for rc in (r.get("expected") or []):
            s = rc_stats.setdefault(rc, {"n": 0, "hit": 0})
            s["n"] += 1
            if rc in (r.get("predicted") or []):
                s["hit"] += 1

    print(f"\n{'='*60}\nBATCH RESULT (mode={args.mode}): {exact}/{len(done)} exact | "
          f"avg F1: {avg_f1:.3f} | {elapsed/60:.1f} min", flush=True)
    for rc, s in sorted(rc_stats.items()):
        print(f"  {rc}: {s['hit']}/{s['n']} ({s['hit']/s['n']*100:.0f}%)", flush=True)
    # error tasks
    errs = [tid for tid, r in checkpoint.items() if tid in ids and r.get("error")]
    if errs:
        print(f"  errors: {len(errs)} tasks -> {errs}", flush=True)


if __name__ == "__main__":
    main()
