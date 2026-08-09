#!/usr/bin/env python3
"""
Swarm Knowledge Base — 初始化 (SQLite)

用法:
  python init_db.py                  # 初始化默认路径的 DB
  python init_db.py --path custom.db # 指定路径
  python init_db.py --stats          # 查看统计
  python init_db.py --reset          # 删除重建
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_DB = str(REPO_ROOT / "swarm_knowledge.db")


def main():
    parser = argparse.ArgumentParser(description="Swarm Knowledge Base Init (SQLite)")
    parser.add_argument("--path", default=DEFAULT_DB, help=f"DB path (default: {DEFAULT_DB})")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--reset", action="store_true", help="Delete and recreate")
    args = parser.parse_args()

    if args.reset and os.path.exists(args.path):
        os.remove(args.path)
        print(f"  🗑 Deleted {args.path}")

    from src import SwarmDB
    from src.governance.engine import run_promotion_cycle

    db = SwarmDB(args.path)

    # 总是运行 migrations（IF NOT EXISTS 保证幂等）
    print(f"📦 Running migrations on {args.path}...")
    db.init()
    print("  ✅ Schema + seed data loaded")

    if args.stats:
        stats = db.stats()
        print(f"\n📊 {args.path}")
        print(f"  Knowledge entries: {stats['knowledge_total']}")
        for level, count in sorted(stats["by_level"].items()):
            label = {1: "D-Data", 2: "I-Info", 3: "K-Knowledge", 4: "W-Wisdom"}.get(level, f"L{level}")
            print(f"    {label}: {count}")
        print(f"  Ontology concepts: {stats['concepts']}")
        print(f"  Ontology relations: {stats['relations']}")
        print(f"  Active rules: {stats['rules_active']}")

        print(f"\n  By type:")
        for k, v in sorted(stats["by_type"].items(), key=lambda x: -x[1]):
            print(f"    {k}: {v}")

        if stats["by_domain"]:
            print(f"\n  By domain:")
            for k, v in sorted(stats["by_domain"].items(), key=lambda x: -x[1]):
                print(f"    {k}: {v}")

    if not args.stats:
        # Run a governance cycle on init
        promoted = run_promotion_cycle(db)
        if promoted["promoted"]:
            print(f"  📈 Promoted: {promoted['promoted']} entries")

    db.close()
    print("✅ Done")


if __name__ == "__main__":
    main()
