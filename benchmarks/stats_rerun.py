"""BountyBench Detect 档统计性重跑: N 次独立运行 → 命中率/方差/对比。
用法: .venv/bin/python -u -m benchmarks.stats_rerun --rounds 5 --modes both
"""
import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict

REPO = os.path.dirname(os.path.abspath(__file__))


def run_once(mode: str, bounties: str, round_no: int) -> dict:
    """跑一次完整运行, 返回 {bounty_x: {exploited, ...}}"""
    log = f"/tmp/stats_{mode}_r{round_no}.log"
    if "--systems" in bounties:
        # 库型模式
        subprocess.run(
            [sys.executable, "-u", "-m", "benchmarks.library_pilot",
             "--systems", bounties.replace("--systems", "").strip(), "--mode", mode],
            cwd=os.path.dirname(REPO), stdout=open(log, "w"), stderr=subprocess.STDOUT,
            timeout=900)
        out = os.path.join(REPO, f"library_pilot_{mode}.json")
        with open(out) as f:
            return json.load(f)
    subprocess.run(
        [sys.executable, "-u", "-m", "benchmarks.bountybench_pilot",
         "--bounties", bounties, "--mode", mode],
        cwd=os.path.dirname(REPO), stdout=open(log, "w"), stderr=subprocess.STDOUT,
        timeout=900)
    # 读产物
    out = os.path.join(REPO, f"bountybench_pilot_{mode}.json")
    with open(out) as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--bounties", default="0,1,2")
    ap.add_argument("--modes", default="both", choices=["single", "swarm", "both"])
    args = ap.parse_args()

    modes = ["single", "swarm"] if args.modes == "both" else [args.modes]
    if args.bounties.startswith("--systems"):
        # 库型: key 是系统名
        sys_names = args.bounties.replace("--systems", "").strip()
        bids = [s for s in sys_names.split(",") if s]
        label = "library"
    else:
        bids = [f"bounty_{b}" for b in args.bounties.split(",")]
        label = "detect"

    # 汇总: mode -> bounty -> [results]
    stats = {m: {b: [] for b in bids} for m in modes}
    per_round = []

    t_start = time.time()
    for rnd in range(1, args.rounds + 1):
        round_res = {"round": rnd}
        print(f"\n=== 第 {rnd}/{args.rounds} 轮 ===", flush=True)
        for mode in modes:
            print(f"  -- {mode} --", flush=True)
            res = run_once(mode, args.bounties, rnd)
            for b in bids:
                v = res.get(b, {})
                ok = bool(v.get("exploited"))
                stats[mode][b].append(ok)
                round_res[f"{mode}:{b}"] = ok
                print(f"    {b}: {'✅' if ok else '❌'} ({v.get('detail', '')[:50]})", flush=True)
        per_round.append(round_res)

    elapsed = time.time() - t_start
    print(f"\n=== 统计汇总 ({elapsed/60:.1f} 分钟) ===")
    summary = {"rounds": args.rounds, "bounties": bids, "modes": modes,
               "per_round": per_round, "stats": {}}
    for mode in modes:
        print(f"\n[{mode}]")
        total_hits = 0
        total_runs = 0
        for b in bids:
            hits = sum(stats[mode][b])
            n = len(stats[mode][b])
            total_hits += hits
            total_runs += n
            print(f"  {b}: {hits}/{n} = {hits/n:.0%}")
        print(f"  合计: {total_hits}/{total_runs} = {total_hits/total_runs:.0%}")
        summary["stats"][mode] = {
            "per_bounty": {b: {"hits": sum(stats[mode][b]), "total": len(stats[mode][b])} for b in bids},
            "total_hits": total_hits, "total_runs": total_runs,
            "accuracy": total_hits / total_runs if total_runs else 0,
        }

    out = os.path.join(REPO, f"stats_rerun_{label}.json")
    with open(out, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
