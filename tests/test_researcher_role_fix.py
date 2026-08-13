"""researcher 角色 CHECK 约束修复回归测试 (2026-08-12)。

背景: migration 016 只给 task_skill_index / model_profiles 加了 researcher,
漏了 agent_profiles.role CHECK → researcher worker 注册即崩
(sqlite3.IntegrityError: CHECK constraint failed: role IN (...)).
修复: db.py 增加 _ensure_agent_profiles_research_role(), init() 时重建表。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB  # noqa: E402
from src.swarm.lifecycle import AgentLifecycle  # noqa: E402


class ResearcherRoleFixTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # 干净库
        self.db = SwarmDB(self.db_path)
        self.db.init()

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_agent_profiles_check_contains_researcher(self):
        ddl = self.db.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_profiles'"
        )
        self.assertIn("'researcher'", ddl["sql"])

    def test_researcher_worker_registers(self):
        # 需要真实 run 满足 FK
        self.db.execute(
            """INSERT INTO swarm_runs (run_id, swarm_name, intent, target_type, target_id, status)
               VALUES ('smoke-run-0001', 'smoke', 'research', 'unknown', 'x', 'running')"""
        )
        life = AgentLifecycle(self.db, agent_id="smoke-researcher-01", run_id="smoke-run-0001")
        life.register(role="researcher", capabilities=["curl", "python3", "rg"])
        row = self.db.fetch_one(
            "SELECT role FROM agent_profiles WHERE agent_id=?", ("smoke-researcher-01",)
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["role"], "researcher")
        # 清理
        life.deregister()

    def test_existing_rows_preserved_across_rebuild(self):
        # 预置数据 → 再次 init (触发重建路径) → 数据保留
        self.db.execute(
            """INSERT INTO agent_profiles (agent_id, agent_name, role)
               VALUES ('keep-01', 'keep-01', 'scanner')"""
        )
        self.db.init()  # 幂等重建
        n = self.db.fetch_one("SELECT COUNT(*) AS c FROM agent_profiles")["c"]
        self.assertEqual(n, 1)
        row = self.db.fetch_one("SELECT role FROM agent_profiles WHERE agent_id='keep-01'")
        self.assertEqual(row["role"], "scanner")


if __name__ == "__main__":
    unittest.main()
