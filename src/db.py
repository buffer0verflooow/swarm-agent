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
