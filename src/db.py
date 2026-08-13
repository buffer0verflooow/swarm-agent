"""
Swarm Knowledge DB — SQLite 数据库层

轻量级封装，替代 PostgreSQL + Memgraph 双数据库。
单文件 .db，零配置，拷贝即分享。

用法:
    from swarm_knowledge.db import SwarmDB
    db = SwarmDB("swarm_knowledge.db")
    db.init()  # 首次自动建表 + 种子数据
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

_log = logging.getLogger("swarm_knowledge.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class SwarmDB:
    """SQLite 数据库封装"""

    def __init__(self, db_path: str = "swarm_knowledge.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def init(self) -> bool:
        """初始化数据库：按顺序运行所有 migration 文件
        
        幂等设计: 所有 migration 使用 IF NOT EXISTS 或 ALTER TABLE（SQLite 自动跳过重复列）。
        对于不支持 IF NOT EXISTS 的旧式 CREATE INDEX，捕获 OperationalError 跳过。
        """
        if not MIGRATIONS_DIR.exists():
            _log.warning("Migrations directory not found: %s", MIGRATIONS_DIR)
            return False

        migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not migrations:
            _log.warning("No migration files found in %s", MIGRATIONS_DIR)
            return False

        for mig in migrations:
            schema_sql = mig.read_text(encoding="utf-8")
            # 逐条执行，跳过已存在的对象
            import re as _re
            # Split on semicolons followed by newline (statement boundary)
            statements = _re.split(r';\s*\n', schema_sql)
            for stmt in statements:
                # Strip comment lines (lines starting with --)
                lines = stmt.split('\n')
                code_lines = [l for l in lines if not l.strip().startswith('--')]
                code = '\n'.join(code_lines).strip()
                if not code:
                    continue  # Pure comment or empty
                try:
                    self.conn.execute(code)
                except sqlite3.OperationalError as e:
                    err = str(e)
                    if 'already exists' in err or 'duplicate column' in err:
                        continue  # 幂等，跳过
                    raise  # 真正的错误，向上抛
            self.conn.commit()
            _log.info("Applied migration: %s", mig.name)

        self._ensure_spawn_request_schema()
        self._ensure_validation_queue_schema()
        self._ensure_research_intent_schema()
        self._ensure_agent_tasks_research_schema()
        self._ensure_agent_profiles_research_role()
        _log.info("Database initialized: %s (%d migrations)", self.db_path, len(migrations))
        return True

    def _table_exists(self, table_name: str) -> bool:
        row = self.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return row is not None

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        if not self._table_exists(table_name):
            return False
        return any(row["name"] == column_name for row in self.fetch_all(f"PRAGMA table_info({table_name})"))


    def _ensure_validation_queue_schema(self) -> None:
        """G3 (2026-08-11): 幂等修复 validation_queue 状态机。

        旧库 (migration 004 已应用) 的 CHECK 约束不含 'inconclusive', 导致
        inconclusive verdict 只能写 'verified' (状态失真 + 污染数据)。本函数:
        1. 检测表 DDL 是否已含 'inconclusive', 缺则重建表 (SQLite 无法改 CHECK)
        2. 清洗存量脏数据: verdict='inconclusive' 但 status='verified' → 'inconclusive'
        """
        if not self._table_exists("validation_queue"):
            return
        ddl = self.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='validation_queue'"
        )
        if ddl and "inconclusive" in (ddl["sql"] or ""):
            # 表结构已含 inconclusive, 只需清洗脏数据 (幂等)
            self.conn.execute(
                """UPDATE validation_queue
                   SET status = 'inconclusive', updated_at = datetime('now')
                   WHERE verdict = 'inconclusive' AND status = 'verified'"""
            )
            self.conn.commit()
            return

        # 重建表: 新 CHECK 含 inconclusive
        with self.transaction():
            self.conn.execute("ALTER TABLE validation_queue RENAME TO validation_queue_old")
            self.conn.execute(
                """CREATE TABLE validation_queue (
                    validation_id        TEXT PRIMARY KEY,
                    knowledge_id        TEXT NOT NULL REFERENCES knowledge_entries(id) ON DELETE CASCADE,
                    run_id              TEXT REFERENCES swarm_runs(run_id) ON DELETE SET NULL,
                    requested_by        TEXT NOT NULL,
                    assigned_to         TEXT,
                    status              TEXT DEFAULT 'pending'
                                        CHECK (status IN ('pending','assigned','validating','verified','refuted','inconclusive','timeout')),
                    priority            INTEGER DEFAULT 50,
                    evidence_hash       TEXT,
                    original_content    TEXT,
                    verdict             TEXT,
                    verdict_reason      TEXT,
                    validated_at        TEXT,
                    created_at          TEXT DEFAULT (datetime('now')),
                    updated_at          TEXT DEFAULT (datetime('now'))
                )"""
            )
            self.conn.execute(
                """INSERT INTO validation_queue (validation_id, knowledge_id, run_id,
                                                 requested_by, assigned_to, status,
                                                 priority, evidence_hash, original_content,
                                                 verdict, verdict_reason, validated_at,
                                                 created_at, updated_at)
                   SELECT v.validation_id, v.knowledge_id, v.run_id,
                          v.requested_by, v.assigned_to, v.status,
                          v.priority, v.evidence_hash, v.original_content,
                          v.verdict, v.verdict_reason, v.validated_at,
                          v.created_at, v.updated_at
                   FROM validation_queue_old v
                   JOIN knowledge_entries ke ON ke.id = v.knowledge_id"""
            )
            self.conn.execute("DROP TABLE validation_queue_old")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_vq_status    ON validation_queue(status)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_vq_knowledge ON validation_queue(knowledge_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_vq_priority ON validation_queue(priority DESC)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_vq_run       ON validation_queue(run_id)")
        # 清洗存量脏数据
        self.conn.execute(
            """UPDATE validation_queue
               SET status = 'inconclusive', updated_at = datetime('now')
               WHERE verdict = 'inconclusive' AND status = 'verified'"""
        )
        self.conn.commit()
        _log.info("validation_queue rebuilt with 'inconclusive' status + dirty data cleaned")

    def _ensure_research_intent_schema(self) -> None:
        """research 产品线 (2026-08-12, migration 016): swarm_runs.intent CHECK
        加入 'research'。

        SQLite 无法修改 CHECK 约束, 与 validation_queue 重建同一模式: 检测
        DDL, 缺 'research' 时重建表并保留全部列/索引/数据。

        注意: SQLite 在 ALTER TABLE RENAME 时即使 foreign_keys=OFF 也会改写
        其他表的引用, 所以不能先 RENAME 旧表 (子表 FK 会指向 swarm_runs_old)。
        采用: 新建 swarm_runs_new → 拷贝 → DROP 旧表 → RENAME new, 子表引用
        保持指向 'swarm_runs' 不变。重建期间关闭 FK 使 DROP 不被引用拦截。
        """
        if not self._table_exists("swarm_runs"):
            return
        ddl = self.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='swarm_runs'"
        )
        if ddl and "'research'" in (ddl["sql"] or ""):
            return

        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    """CREATE TABLE swarm_runs_new (
                        run_id          TEXT PRIMARY KEY,
                        swarm_name      TEXT NOT NULL,
                        intent          TEXT NOT NULL CHECK (intent IN ('recon','exploit','analyze','defend','report','research','custom')),
                        target_type     TEXT NOT NULL CHECK (target_type IN ('ip','binary','apk','webapp','domain','network','unknown')),
                        target_id       TEXT NOT NULL,
                        status          TEXT DEFAULT 'running' CHECK (status IN ('pending','running','completed','failed','cancelled')),
                        config          TEXT DEFAULT '{}',
                        stats           TEXT DEFAULT '{}',
                        started_at      TEXT DEFAULT (datetime('now')),
                        ended_at        TEXT,
                        created_at      TEXT DEFAULT (datetime('now')),
                        updated_at      TEXT DEFAULT (datetime('now')),
                        tokens_spent INTEGER DEFAULT 0, budget_strategy TEXT DEFAULT 'balanced'
                            CHECK (budget_strategy IN ('breadth','depth','balanced','exploit')),
                        token_budget INTEGER DEFAULT 100000, conversation_summary TEXT DEFAULT '',
                        summary_updated_at TEXT, strategy_version INTEGER DEFAULT 0
                    )"""
                )
                self.conn.execute(
                    """INSERT INTO swarm_runs_new (run_id, swarm_name, intent, target_type, target_id,
                                                   status, config, stats, started_at, ended_at,
                                                   created_at, updated_at, tokens_spent, budget_strategy,
                                                   token_budget, conversation_summary, summary_updated_at,
                                                   strategy_version)
                       SELECT run_id, swarm_name, intent, target_type, target_id,
                              status, config, stats, started_at, ended_at,
                              created_at, updated_at, tokens_spent, budget_strategy,
                              token_budget, conversation_summary, summary_updated_at,
                              strategy_version
                       FROM swarm_runs"""
                )
                self.conn.execute("DROP TABLE swarm_runs")
                self.conn.execute("ALTER TABLE swarm_runs_new RENAME TO swarm_runs")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_swarm_runs_swarm ON swarm_runs(swarm_name)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_swarm_runs_intent ON swarm_runs(intent)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_swarm_runs_status ON swarm_runs(status)")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
        _log.info("swarm_runs rebuilt with 'research' intent")

    def _ensure_agent_tasks_research_schema(self) -> None:
        """research 产品线 (2026-08-12, migration 016): agent_tasks.task_type
        CHECK 加入 'research'。

        与 swarm_runs 重建同一模式 (SQLite 不能改 CHECK): 新建 agent_tasks_new →
        拷贝 → DROP 旧表 → RENAME, 子表 (agent_delegations 等) 对 agent_tasks
        的引用保持指向原名; 重建期间关闭 FK, 之后恢复。
        """
        if not self._table_exists("agent_tasks"):
            return
        ddl = self.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_tasks'"
        )
        if ddl and "'research'" in (ddl["sql"] or ""):
            return

        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    """CREATE TABLE agent_tasks_new (
                        task_id         TEXT PRIMARY KEY,
                        run_id          TEXT NOT NULL REFERENCES swarm_runs(run_id) ON DELETE CASCADE,
                        agent_id        TEXT REFERENCES agent_profiles(agent_id) ON DELETE SET NULL,
                        parent_task_id  TEXT REFERENCES agent_tasks_new(task_id) ON DELETE SET NULL,
                        task_type       TEXT NOT NULL CHECK (task_type IN ('scan','analyze','exploit','report','research','subtask','custom')),
                        task_intent     TEXT,
                        focus_params    TEXT DEFAULT '{}',
                        iteration       INTEGER DEFAULT 1,
                        status          TEXT DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','timeout')),
                        result_summary  TEXT DEFAULT '{}',
                        token_cost      INTEGER DEFAULT 0,
                        started_at      TEXT,
                        ended_at        TEXT,
                        created_at      TEXT DEFAULT (datetime('now')),
                        updated_at      TEXT DEFAULT (datetime('now')),
                        estimated_tokens INTEGER DEFAULT 0, required_role TEXT,
                        priority INTEGER DEFAULT 50, claimed_at TEXT, signal_key TEXT,
                        claim_count INTEGER DEFAULT 0, model_profile_id TEXT,
                        base_priority INTEGER, graph_id TEXT, task_key TEXT,
                        depends_on_keys TEXT DEFAULT '[]', acceptance_criteria TEXT DEFAULT '[]',
                        tool_allowlist TEXT DEFAULT '[]', acceptance_status TEXT
                            DEFAULT 'pending' CHECK (acceptance_status IN ('pending', 'accepted', 'rejected'))
                    )"""
                )
                self.conn.execute(
                    """INSERT INTO agent_tasks_new (task_id, run_id, agent_id, parent_task_id,
                                                    task_type, task_intent, focus_params, iteration,
                                                    status, result_summary, token_cost, started_at,
                                                    ended_at, created_at, updated_at, estimated_tokens,
                                                    required_role, priority, claimed_at, signal_key,
                                                    claim_count, model_profile_id, base_priority,
                                                    graph_id, task_key, depends_on_keys,
                                                    acceptance_criteria, tool_allowlist, acceptance_status)
                       SELECT task_id, run_id, agent_id, parent_task_id,
                              task_type, task_intent, focus_params, iteration,
                              status, result_summary, token_cost, started_at,
                              ended_at, created_at, updated_at, estimated_tokens,
                              required_role, priority, claimed_at, signal_key,
                              claim_count, model_profile_id, base_priority,
                              graph_id, task_key, depends_on_keys,
                              acceptance_criteria, tool_allowlist, acceptance_status
                       FROM agent_tasks"""
                )
                self.conn.execute("DROP TABLE agent_tasks")
                self.conn.execute("ALTER TABLE agent_tasks_new RENAME TO agent_tasks")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_run      ON agent_tasks(run_id)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent    ON agent_tasks(agent_id)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_parent   ON agent_tasks(parent_task_id)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_status   ON agent_tasks(status)")
                self.conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_agent_tasks_market
                       ON agent_tasks(run_id, status, required_role, priority DESC, created_at)"""
                )
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_claimed_at ON agent_tasks(claimed_at)")
                self.conn.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_signal_active
                       ON agent_tasks(run_id, signal_key)
                       WHERE signal_key IS NOT NULL AND status IN ('pending', 'running')"""
                )
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_tasks_model_profile ON agent_tasks(model_profile_id)")
                self.conn.execute("CREATE INDEX IF NOT EXISTS idx_at_graph ON agent_tasks(graph_id, status)")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
        _log.info("agent_tasks rebuilt with 'research' task_type")

    def _ensure_agent_profiles_research_role(self) -> None:
        """research 产品线 (2026-08-12, migration 016): agent_profiles.role
        CHECK 加入 'researcher'。

        migration 016 只给 task_skill_index / model_profiles 加了 researcher,
        漏了 agent_profiles 的 role CHECK → researcher worker 注册即崩
        (sqlite3.IntegrityError: CHECK constraint failed: role IN (...))。

        与 swarm_runs 重建同一模式: 检测 DDL, 缺 'researcher' 时新建
        agent_profiles_new → 拷贝 → DROP 旧表 → RENAME, 子表
        (agent_heartbeats 等) 对 agent_profiles 的 FK 引用保持指向原名;
        重建期间关闭 FK 使 DROP 不被引用拦截。
        """
        if not self._table_exists("agent_profiles"):
            return
        ddl = self.fetch_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_profiles'"
        )
        if ddl and "'researcher'" in (ddl["sql"] or ""):
            return

        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    """CREATE TABLE agent_profiles_new (
                        agent_id        TEXT PRIMARY KEY,
                        agent_name      TEXT UNIQUE NOT NULL,
                        role            TEXT NOT NULL CHECK (role IN ('scanner','analyst','exploiter','reporter','orchestrator','researcher','custom')),
                        capabilities    TEXT DEFAULT '[]',
                        default_tools   TEXT DEFAULT '[]',
                        model_preference TEXT,
                        status          TEXT DEFAULT 'active' CHECK (status IN ('active','idle','deprecated')),
                        metadata        TEXT DEFAULT '{}',
                        created_at      TEXT DEFAULT (datetime('now')),
                        token_budget_limit INTEGER,
                        model_profile_id TEXT
                    )"""
                )
                self.conn.execute(
                    """INSERT INTO agent_profiles_new (agent_id, agent_name, role,
                                                        capabilities, default_tools,
                                                        model_preference, status, metadata,
                                                        created_at, token_budget_limit,
                                                        model_profile_id)
                       SELECT agent_id, agent_name, role,
                              capabilities, default_tools,
                              model_preference, status, metadata,
                              created_at, token_budget_limit,
                              model_profile_id
                       FROM agent_profiles"""
                )
                self.conn.execute("DROP TABLE agent_profiles")
                self.conn.execute("ALTER TABLE agent_profiles_new RENAME TO agent_profiles")
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
        _log.info("agent_profiles rebuilt with 'researcher' role")

    def _ensure_spawn_request_schema(self) -> None:
        """Apply idempotent spawn_requests schema fixes without new migrations."""
        if not self._table_exists("spawn_requests"):
            return

        if not self._column_exists("spawn_requests", "claimed_by"):
            self.conn.execute("ALTER TABLE spawn_requests ADD COLUMN claimed_by TEXT")
        if not self._column_exists("spawn_requests", "dedup_key"):
            self.conn.execute("ALTER TABLE spawn_requests ADD COLUMN dedup_key TEXT")

        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sr_claimed_by ON spawn_requests(claimed_by)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sr_dedup_key ON spawn_requests(run_id, dedup_key)"
        )
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_sr_pending_dedup_unique
               ON spawn_requests(run_id, dedup_key, status)
               WHERE status = 'pending' AND dedup_key IS NOT NULL"""
        )
        self.conn.commit()

    @contextmanager
    def transaction(self):
        """事务上下文"""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetch_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def fetch_all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    # ── 便捷方法 ──

    def insert_knowledge(
        self,
        content: str,
        title: str = "",
        knowledge_type: str = "observation",
        level: int = 1,
        domain: str = "general",
        source_agent: str = "knowledge-extractor",
        tags: Optional[List[str]] = None,
        knowledge_intent: str = "understand",
        trust_vector: Optional[Dict[str, float]] = None,
    ) -> str:
        """插入一条知识条目"""
        entry_id = str(uuid.uuid4())
        tv = trust_vector or {"logic_soundness": 0.6, "base_confidence": 0.7, "cross_validation": 0.0}

        self.conn.execute(
            """INSERT INTO knowledge_entries
               (id, level, knowledge_type, content, title, source_agent,
                domain, knowledge_intent, trust_vector, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id, level, knowledge_type, content, title, source_agent,
                domain, knowledge_intent, json.dumps(tv), json.dumps(tags or []),
            ),
        )
        self.conn.commit()

        # FTS index
        self.conn.execute(
            "INSERT INTO knowledge_entries_fts(rowid, title, content) "
            "VALUES ((SELECT rowid FROM knowledge_entries WHERE id=?), ?, ?)",
            (entry_id, title, content),
        )
        self.conn.commit()
        return entry_id

    def search_knowledge(
        self,
        query: str,
        domain: Optional[str] = None,
        level: Optional[int] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """全文搜索知识条目"""
        # Sanitize FTS5 query
        import re as _re
        words = _re.findall(r'[a-zA-Z0-9_\u4e00-\u9fff]{2,}', query[:200])
        safe_query = " OR ".join(words[:5]) if words else ""
        if not safe_query:
            return []

        conditions = ["ke.status = 'active'"]
        params: List[Any] = [safe_query]

        if domain:
            conditions.append("ke.domain = ?")
            params.append(domain)
        if level:
            conditions.append("ke.level >= ?")
            params.append(level)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT ke.id, ke.title, ke.level, ke.knowledge_type,
                   ke.domain, ke.content, ke.tags, ke.trust_vector,
                   ke.created_at,
                   snippet(knowledge_entries_fts, 1, '<b>', '</b>', '...', 40) AS snippet
            FROM knowledge_entries_fts fts
            JOIN knowledge_entries ke ON fts.rowid = ke.rowid
            WHERE knowledge_entries_fts MATCH ? AND {where}
            ORDER BY rank
            LIMIT ?
        """
        params.append(limit)

        rows = self.fetch_all(sql, tuple(params))
        return [dict(r) for r in rows]

    def get_by_domain(self, domain: str, level: int = 2, limit: int = 20) -> List[Dict[str, Any]]:
        """按领域获取知识"""
        rows = self.fetch_all(
            """SELECT id, title, level, knowledge_type, content, tags, trust_vector
               FROM knowledge_entries
               WHERE domain = ? AND level >= ? AND status = 'active'
               ORDER BY level DESC, created_at DESC
               LIMIT ?""",
            (domain, level, limit),
        )
        return [dict(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        """数据库统计"""
        return {
            "knowledge_total": self.fetch_one("SELECT COUNT(*) AS cnt FROM knowledge_entries")["cnt"],
            "by_level": {
                row["level"]: row["cnt"]
                for row in self.fetch_all("SELECT level, COUNT(*) AS cnt FROM knowledge_entries GROUP BY level")
            },
            "by_type": {
                row["knowledge_type"]: row["cnt"]
                for row in self.fetch_all("SELECT knowledge_type, COUNT(*) AS cnt FROM knowledge_entries GROUP BY knowledge_type")
            },
            "by_domain": {
                row["domain"]: row["cnt"]
                for row in self.fetch_all("SELECT domain, COUNT(*) AS cnt FROM knowledge_entries WHERE domain IS NOT NULL GROUP BY domain")
            },
            "concepts": self.fetch_one("SELECT COUNT(*) AS cnt FROM ontology_concepts")["cnt"],
            "relations": self.fetch_one("SELECT COUNT(*) AS cnt FROM ontology_relations")["cnt"],
            "rules_active": self.fetch_one("SELECT COUNT(*) AS cnt FROM distilled_rules WHERE is_active=1")["cnt"],
        }

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ── Singleton 方便使用 ──

_default_db: Optional[SwarmDB] = None


def get_db(db_path: Optional[str] = None) -> SwarmDB:
    """获取默认数据库实例"""
    global _default_db
    if _default_db is None:
        path = db_path or os.getenv("SWARM_DB_PATH", str(Path.home() / "workspace" / "research" / "swarm-knowledge" / "swarm_knowledge.db"))
        _default_db = SwarmDB(path)
        if not os.path.exists(path):
            _default_db.init()
    return _default_db
