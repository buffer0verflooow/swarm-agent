"""
模型对照表 (migration 020): 免费池分层测试。

覆盖:
1. migration 020 生效: model_profiles.tier 列 + model_usage_daily 表 + 免费种子
2. resolve_execution_model: free 角色 → opencode 引擎 + 免费模型
3. 免费池轮询: 当日 calls 少的优先 (均衡)
4. 超限降级: 免费池全超限 → 该角色付费 profile
5. paid 角色不受影响: engine='hermes'
6. record_model_usage UPSERT 记账
7. free_pool_status 诊断快照
"""

from __future__ import annotations

import pytest

from src.swarm.model_config import (
    free_pool_status,
    get_model_profile,
    record_model_usage,
    resolve_execution_model,
    upsert_model_profile,
)


def _task(role: str = "scanner", **extra) -> dict:
    task = {"required_role": role, "task_type": "scan"}
    task.update(extra)
    return task


def _clear_usage(db) -> None:
    db.execute("DELETE FROM model_usage_daily")
    db.conn.commit()


# ── 1. migration 020 生效 ────────────────────────────────────────────────

def test_migration_020_schema(db):
    """model_profiles 有 tier 列, model_usage_daily 表存在。"""
    cols = {r["name"] for r in db.fetch_all("PRAGMA table_info(model_profiles)")}
    assert "tier" in cols
    assert db._table_exists("model_usage_daily")


def test_free_pool_seeds_present(db):
    """免费池种子: scanner/data-analyst/reporter/report-writer/content-writer/custom。"""
    rows = db.fetch_all(
        "SELECT role, provider, model FROM model_profiles WHERE tier = 'free'"
    )
    roles = {r["role"] for r in rows}
    assert {"scanner", "data-analyst", "reporter", "custom"} <= roles
    for r in rows:
        assert r["provider"] in ("zenmux", "opencode")


# ── 2. resolve_execution_model ───────────────────────────────────────────

def test_free_role_resolves_to_opencode(db):
    """scanner (free 角色) → engine=opencode + 免费模型。"""
    resolved = resolve_execution_model(db, _task("scanner"))
    assert resolved is not None
    assert resolved["tier"] == "free"
    assert resolved["engine"] == "opencode"
    assert resolved["resolved_model"] in (
        "zenmux/z-ai/glm-5.3-free",
        "opencode/nemotron-3-ultra-free",
    )
    assert resolved["pool_used"] is True
    # 兼容字段保留
    assert resolved["profile_id"]
    assert resolved["provider"]
    assert resolved["model"]


def test_paid_role_uses_hermes_engine(db):
    """无 free 种子的角色 (analyst) → engine=hermes 付费通道。"""
    resolved = resolve_execution_model(db, _task("analyst"))
    assert resolved is not None
    assert resolved["tier"] == "paid"
    assert resolved["engine"] == "hermes"
    assert resolved["pool_used"] is False
    assert resolved["resolved_model"] is None


# ── 3. 免费池轮询 (calls 均衡) ──────────────────────────────────────────

def test_pool_rotates_by_calls(db):
    """记录用量后, 已用的免费模型让位给未用的。"""
    _clear_usage(db)
    # 先记录 glm-5.3-free 大量调用, 使其 calls 最多
    record_model_usage(db, "zenmux/z-ai/glm-5.3-free", tokens=100, calls=50)
    record_model_usage(db, "opencode/nemotron-3-ultra-free", tokens=100, calls=2)

    resolved = resolve_execution_model(db, _task("scanner"))
    assert resolved is not None
    # calls 最少的是 nemotron-3-ultra-free (2 次), 应被选中
    assert resolved["resolved_model"] == "opencode/nemotron-3-ultra-free"


def test_pool_round_robin_returns_to_low_usage(db):
    """无任何用量时, 选 priority 最高的模型。"""
    _clear_usage(db)
    resolved = resolve_execution_model(db, _task("scanner"))
    assert resolved is not None
    assert resolved["tier"] == "free"


# ── 4. 超限降级 ─────────────────────────────────────────────────────────

def test_pool_exhausted_falls_back_to_paid(db, monkeypatch):
    """免费池全超限 → 降级该角色付费 profile。"""
    _clear_usage(db)
    # 让所有免费模型都达到 calls 上限 (默认 300)
    free_models = [r["model"] for r in db.fetch_all(
        "SELECT model FROM model_profiles WHERE tier = 'free'"
    )]
    for m in free_models:
        record_model_usage(db, m, tokens=10_000_000, calls=9999)

    resolved = resolve_execution_model(db, _task("scanner"))
    assert resolved is not None
    assert resolved["tier"] == "paid"
    assert resolved["engine"] == "hermes"
    assert resolved["pool_used"] is False


def test_pool_exhausted_falls_back_to_custom_paid(db):
    """角色自身无 paid profile 时 → custom 付费兜底。"""
    _clear_usage(db)
    # 删除 scanner 的 paid profile, 只留 free
    db.execute(
        "UPDATE model_profiles SET enabled = 0 WHERE role = 'scanner' AND tier = 'paid'"
    )
    free_models = [r["model"] for r in db.fetch_all(
        "SELECT model FROM model_profiles WHERE tier = 'free'"
    )]
    for m in free_models:
        record_model_usage(db, m, tokens=10_000_000, calls=9999)
    db.conn.commit()

    resolved = resolve_execution_model(db, _task("scanner"))
    assert resolved is not None
    assert resolved["tier"] == "paid"
    assert resolved["engine"] == "hermes"


def test_custom_role_free_pool(db):
    """custom 角色默认 profile 是 free 种子 → 走免费池。"""
    resolved = resolve_execution_model(db, _task("custom"))
    assert resolved is not None
    assert resolved["tier"] == "free"
    assert resolved["engine"] == "opencode"


# ── 5. record_model_usage ────────────────────────────────────────────────

def test_record_model_usage_upsert(db):
    """同模型同日重复记账累加。"""
    _clear_usage(db)
    record_model_usage(db, "zenmux/z-ai/glm-5.3-free", tokens=100, calls=1)
    record_model_usage(db, "zenmux/z-ai/glm-5.3-free", tokens=200, calls=1)

    row = db.fetch_one(
        "SELECT tokens, calls FROM model_usage_daily WHERE model_key = 'zenmux/z-ai/glm-5.3-free'"
    )
    assert row["tokens"] == 300
    assert row["calls"] == 2


def test_record_model_usage_empty_key_noop(db):
    """空 model_key 不写库。"""
    _clear_usage(db)
    record_model_usage(db, "", tokens=1)
    assert db.fetch_one("SELECT COUNT(*) AS c FROM model_usage_daily")["c"] == 0


# ── 6. free_pool_status ─────────────────────────────────────────────────

def test_free_pool_status_snapshot(db):
    _clear_usage(db)
    record_model_usage(db, "z-ai/glm-5.3-free", tokens=1000, calls=5)
    status = free_pool_status(db)
    assert status, "免费池应有种子模型"
    by_model = {s["model"]: s for s in status}
    entry = by_model["z-ai/glm-5.3-free"]
    assert entry["tokens_today"] == 1000
    assert entry["calls_today"] == 5
    assert entry["limit_calls"] >= 5
    assert entry["available"] is True


# ── 7. 环境变量限额覆盖 ─────────────────────────────────────────────────

def test_env_limit_override(db, monkeypatch):
    """SWARM_FREE_DAILY_CALLS=1 时, 所有免费模型用满后降级付费。"""
    _clear_usage(db)
    monkeypatch.setenv("SWARM_FREE_DAILY_CALLS", "1")
    resolved1 = resolve_execution_model(db, _task("scanner"))
    assert resolved1 is not None and resolved1["tier"] == "free"
    # 所有免费模型各记 1 次调用 → 全部超限 → 降级
    free_models = [r["model"] for r in db.fetch_all(
        "SELECT model FROM model_profiles WHERE tier = 'free'"
    )]
    for m in free_models:
        record_model_usage(db, m, tokens=0, calls=1)
    resolved2 = resolve_execution_model(db, _task("scanner"))
    assert resolved2 is not None
    assert resolved2["tier"] == "paid"
    assert resolved2["engine"] == "hermes"
