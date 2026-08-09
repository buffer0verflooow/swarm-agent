"""
Shared pytest fixtures for the swarm-knowledge test suite.

Some test modules were written before a shared conftest existed and each
declared ``db`` / ``run_id`` fixtures that were never provided at collection
time (they errored with "fixture 'db' not found"). This file supplies them so
the whole suite runs: ``.venv/bin/python -m pytest tests/ -q``.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import SwarmDB


@pytest.fixture()
def db(tmp_path):
    database = SwarmDB(str(tmp_path / "test.db"))
    assert database.init(), "migrations should apply"
    yield database
    database.close()


@pytest.fixture()
def run_id(db) -> str:
    rid = str(uuid.uuid4())
    db.execute(
        """INSERT INTO swarm_runs
           (run_id, swarm_name, intent, target_type, target_id, status)
           VALUES (?, 'test-swarm', 'recon', 'webapp', 'demo.test', 'running')""",
        (rid,),
    )
    db.conn.commit()
    return rid


@pytest.fixture()
def db_path(tmp_path) -> str:
    """Path to an initialized SQLite DB for CLI-level tests."""
    path = str(tmp_path / "cli.db")
    database = SwarmDB(path)
    assert database.init(), "migrations should apply"
    database.close()
    return path
