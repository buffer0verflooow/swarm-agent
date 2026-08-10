"""pwn_college 统计重跑: 5 轮 × (single|swarm)"""
import sys, os, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pwn_college_runner import run_executor, analyze_swarm

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats_rerun_pwn.json")

results = {"single": [], "swarm": []}
for mode in ["single", "swarm"]:
    for i in range(ROUNDS):
        t0 = time.time()
        if mode == "single":
            r = run_executor(None, rounds=4)
        else:
            r = analyze_swarm(None)
        dt = time.time() - t0
        ok = r.get("exploited", False)
        results[mode].append({"run": i + 1, "ok": ok, "detail": r.get("detail", "")[:200], "secs": round(dt, 1)})
        print(f"[{mode}] run {i+1}/{ROUNDS}: {'✅' if ok else '❌'} ({round(dt)}s)", flush=True)

with open(OUT, "w") as f:
    json.dump(results, f, ensure_ascii=False, indent=1)

print("\n=== 统计 ===")
for mode, arr in results.items():
    n_ok = sum(1 for x in arr if x["ok"])
    print(f"{mode}: {n_ok}/{len(arr)} ({100 * n_ok / len(arr):.0f}%)")
