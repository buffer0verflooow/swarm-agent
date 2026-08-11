"""
回归测试: hypothesis-validation-pipeline 报告 G1/G2/G3 修复 (2026-08-11)

- G3: inconclusive verdict 必须写回队列状态 'inconclusive' (不再记 'verified');
      migration 004 CHECK 含 inconclusive; db.py 幂等重建旧库表 + 清洗存量脏数据
- G2: HIGH (level>=3) 条目 confirmed 必须由 replay_verifier 真实外部复现,
      库内信号仅作线索; 无复现能力时最高判 inconclusive
- G1: confirmed 后自动执行机器可判门 (in_scope/deduplicated), 其余门 blocked,
      假设 validation_status 不再永远卡在 'hypothesis'

运行: .venv/bin/python -m pytest tests/test_verification_gates_fix.py -q
"""

from __future__ import annotations

import json
import uuid

from src.db import SwarmDB
from src.governance.verification import (
    auto_enqueue_validations,
    process_validation_queue,
)


def _insert_vuln(db, run_id, *, level=2, content="CVE-2024-9999 at http://example.test with nuclei evidence", tags=None):
    entry_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent, source_run_id,
            domain, knowledge_intent, tags, trust_vector)
           VALUES (?, ?, 'vulnerability', ?, 'vuln target', 'test-agent', ?,
                   'security', 'attack', ?, ?)""",
        (
            entry_id,
            level,
            content,
            run_id,
            json.dumps(tags or []),
            '{"logic_soundness":0.9,"base_confidence":0.9,"cross_validation":0.8}',
        ),
    )
    db.execute(
        """INSERT INTO knowledge_lineage
           (knowledge_id, source_type, source_ref, extraction_method, confidence_contribution)
           VALUES (?, 'agent_execution', ?, 'agent_analysis', 0.9)""",
        (entry_id, json.dumps({"run": run_id})),
    )
    return entry_id


def test_g3_inconclusive_written_as_inconclusive(db, run_id):
    """G3: inconclusive verdict 必须写队列状态 'inconclusive', 禁止记 'verified'"""
    # 无任何可验证特征的内容 → _auto_verify 判 inconclusive
    entry_id = _insert_vuln(db, run_id, content="analyst observed unusual behavior in module X")
    db.conn.commit()

    result = auto_enqueue_validations(db, run_id)
    assert result["enqueued"] == 1

    processed = process_validation_queue(db)
    assert processed["processed"] == 1
    assert processed["inconclusive"] == 1
    assert processed["confirmed"] == 0

    row = db.fetch_one(
        "SELECT status, verdict FROM validation_queue WHERE knowledge_id=?", (entry_id,)
    )
    assert row["verdict"] == "inconclusive"
    assert row["status"] == "inconclusive", "G3: inconclusive 必须记 'inconclusive' 而非 'verified'"
    print("  ✅ G3: inconclusive verdict → queue status 'inconclusive'")


def test_g3_inconclusive_not_requeued(db, run_id):
    """G3: 处理为 inconclusive 的条目不再被 auto_enqueue 重复入队"""
    _insert_vuln(db, run_id, content="plain observation without verifiable features")
    db.conn.commit()

    auto_enqueue_validations(db, run_id)
    processed = process_validation_queue(db)
    assert processed["processed"] == 1
    assert processed["inconclusive"] == 1

    again = auto_enqueue_validations(db, run_id)
    assert again["enqueued"] == 0, "inconclusive 条目不应被重复入队"
    print("  ✅ G3: inconclusive 条目不重复入队")


def test_g3_schema_rebuild_and_dirty_cleanup(db):
    """G3: db.py ensure 幂等重建旧库表(含 inconclusive CHECK) + 清洗存量脏数据"""
    # 模拟旧库: 重建为不含 inconclusive 的旧 CHECK 表 + 制造脏数据
    db.conn.execute("DROP TABLE IF EXISTS validation_queue")
    db.conn.execute(
        """CREATE TABLE validation_queue (
            validation_id TEXT PRIMARY KEY,
            knowledge_id TEXT NOT NULL,
            run_id TEXT,
            requested_by TEXT NOT NULL,
            assigned_to TEXT,
            status TEXT DEFAULT 'pending'
                CHECK (status IN ('pending','assigned','validating','verified','refuted','timeout')),
            priority INTEGER DEFAULT 50,
            evidence_hash TEXT,
            original_content TEXT,
            verdict TEXT,
            verdict_reason TEXT,
            validated_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    # 造一条真实 knowledge entry, 让脏数据能通过 FK/join 保留下来
    kid = str(uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent,
            domain, knowledge_intent, tags, trust_vector)
           VALUES (?, 2, 'vulnerability', 'legacy content', 'legacy', 'legacy-agent',
                   'security', 'attack', '[]', '{}')""",
        (kid,),
    )
    db.conn.execute(
        """INSERT INTO validation_queue (validation_id, knowledge_id, requested_by, status, verdict, verdict_reason)
           VALUES ('dirty-1', ?, 'legacy', 'verified', 'inconclusive', 'legacy dirty row')""",
        (kid,),
    )
    db.conn.commit()

    db._ensure_validation_queue_schema()

    # 表 DDL 已含 inconclusive
    ddl = db.fetch_one(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='validation_queue'"
    )["sql"]
    assert "inconclusive" in ddl, "重建后 CHECK 必须含 inconclusive"

    # 脏数据被清洗
    row = db.fetch_one("SELECT status FROM validation_queue WHERE validation_id='dirty-1'")
    assert row["status"] == "inconclusive", "存量脏数据 status 必须改回 inconclusive"

    # 幂等: 再跑一次不报错、不清空数据
    db._ensure_validation_queue_schema()
    row = db.fetch_one("SELECT status FROM validation_queue WHERE validation_id='dirty-1'")
    assert row["status"] == "inconclusive"
    print("  ✅ G3: 旧库重建 + 脏数据清洗 + 幂等")


def test_g3_migration004_check_contains_inconclusive():
    """G3: migration 004 的 CHECK 约束声明含 inconclusive"""
    text = open("migrations/004_verification_wisdom.sql", encoding="utf-8").read()
    assert "'inconclusive'" in text
    print("  ✅ G3: migration 004 CHECK 已加 inconclusive")


def test_g2_high_entry_requires_replay(db, run_id):
    """G2: HIGH (level>=3) 条目无 replay_verifier → 最高 inconclusive; 有则 confirmed/refuted"""
    entry_id = _insert_vuln(db, run_id, level=3, content="CVE-2024-9999 http://example.test nuclei")
    db.conn.commit()

    auto_enqueue_validations(db, run_id)

    # 无 replay_verifier → 不 confirmed
    processed = process_validation_queue(db)
    assert processed["processed"] == 1
    assert processed["confirmed"] == 0
    row = db.fetch_one(
        "SELECT status, verdict, verdict_reason FROM validation_queue WHERE knowledge_id=?",
        (entry_id,),
    )
    assert row["verdict"] == "inconclusive"
    assert "复现" in row["verdict_reason"], "原因应提示需外部复现"
    print("  ✅ G2: HIGH 条目无外部复现能力时最高 inconclusive")

    # 有 replay_verifier 且复现成功 → confirmed
    entry2 = _insert_vuln(db, run_id, level=3, content="CVE-2024-9999 http://example.test nuclei")
    db.conn.commit()
    auto_enqueue_validations(db, run_id)
    def ok_verifier(kid, content):
        return True, "curl replay matched (HTTP 200 + header X)"
    processed = process_validation_queue(db, replay_verifier=ok_verifier)
    assert processed["processed"] == 1
    assert processed["confirmed"] == 1
    row = db.fetch_one("SELECT status, verdict FROM validation_queue WHERE knowledge_id=?", (entry2,))
    assert row["status"] == "verified"
    assert row["verdict"] == "confirmed"
    print("  ✅ G2: replay_verifier 复现成功 → confirmed")

    # 有 replay_verifier 但复现失败 → refuted
    entry3 = _insert_vuln(db, run_id, level=3, content="CVE-2024-9999 http://example.test nuclei")
    db.conn.commit()
    auto_enqueue_validations(db, run_id)
    def fail_verifier(kid, content):
        return False, "curl returned 404"
    processed = process_validation_queue(db, replay_verifier=fail_verifier)
    assert processed["processed"] == 1
    assert processed["refuted"] == 1
    row = db.fetch_one("SELECT status, verdict FROM validation_queue WHERE knowledge_id=?", (entry3,))
    assert row["status"] == "refuted"
    assert row["verdict"] == "refuted"
    print("  ✅ G2: replay_verifier 复现失败 → refuted")


def test_g2_low_entry_keeps_heuristic(db, run_id):
    """G2: 非 HIGH 条目保留启发式确认 (库内信号仍可 confirmed)"""
    entry_id = _insert_vuln(db, run_id, level=2, content="CVE-2024-9999 at http://example.test with nuclei evidence")
    db.conn.commit()
    auto_enqueue_validations(db, run_id)
    processed = process_validation_queue(db)
    assert processed["confirmed"] == 1
    row = db.fetch_one("SELECT status, verdict FROM validation_queue WHERE knowledge_id=?", (entry_id,))
    assert row["verdict"] == "confirmed"
    print("  ✅ G2: 低等级条目保留启发式 confirmed")


def test_g1_auto_gates_advance_hypothesis(db, run_id):
    """G1: confirmed 后自动跑机器可判门, 假设不再卡在 'hypothesis'"""
    # out_of_scope 假设 → in_scope fail → negative_knowledge
    entry_id = _insert_vuln(db, run_id, level=2, content="CVE-2024-9999 at http://example.test with nuclei evidence")
    from src.governance.bounty import create_finding_hypothesis
    hyp = create_finding_hypothesis(
        db, entry_id, created_by="auto-verification",
        scope_status="out_of_scope", rationale="auto",
    )
    db.conn.commit()

    auto_enqueue_validations(db, run_id)
    processed = process_validation_queue(db)
    assert processed["confirmed"] == 1

    hyp_row = db.fetch_one(
        "SELECT validation_status FROM finding_hypotheses WHERE hypothesis_id=?",
        (hyp["hypothesis_id"],),
    )
    assert hyp_row["validation_status"] == "negative_knowledge", (
        f"out_of_scope 假设应自动到 negative_knowledge, 实际 {hyp_row['validation_status']}"
    )
    neg = db.fetch_one(
        "SELECT COUNT(*) AS c FROM negative_knowledge WHERE hypothesis_id=?",
        (hyp["hypothesis_id"],),
    )
    assert neg["c"] >= 1
    print("  ✅ G1: out_of_scope → 自动 negative_knowledge")

    # in_scope + 无重复 → 机器门 pass, 其余门 blocked → validating (不再 hypothesis)
    entry2 = _insert_vuln(db, run_id, level=2, content="CVE-2025-1111 at http://other.test with nuclei evidence")
    hyp2 = create_finding_hypothesis(
        db, entry2, created_by="auto-verification",
        scope_status="in_scope", rationale="auto2",
    )
    db.conn.commit()
    auto_enqueue_validations(db, run_id)
    processed = process_validation_queue(db)
    assert processed["confirmed"] == 1

    hyp2_row = db.fetch_one(
        "SELECT validation_status FROM finding_hypotheses WHERE hypothesis_id=?",
        (hyp2["hypothesis_id"],),
    )
    assert hyp2_row["validation_status"] == "validating", (
        f"in_scope 假设应推进到 validating, 实际 {hyp2_row['validation_status']}"
    )
    gates = {
        g["gate_name"]: g["status"]
        for g in db.fetch_all(
            "SELECT gate_name, status FROM finding_validation_gates WHERE hypothesis_id=?",
            (hyp2["hypothesis_id"],),
        )
    }
    assert gates.get("in_scope") == "pass"
    assert gates.get("deduplicated") == "pass"
    assert gates.get("poc_exists") == "blocked", "poc 等门应 blocked 等人工补证据"
    assert gates.get("low_priv_reachable") == "blocked"
    print("  ✅ G1: in_scope 假设 → validating + 机器门自动判定")


def test_g1_auto_gates_skip_human_decided(db, run_id):
    """G1: 机器门不覆盖人工已判定的结果 (只覆盖 pending)"""
    from src.governance.bounty import create_finding_hypothesis, record_gate_result
    entry_id = _insert_vuln(db, run_id, level=2, content="CVE-2026-2222 at http://h.test with nuclei evidence")
    hyp = create_finding_hypothesis(
        db, entry_id, created_by="auto-verification",
        scope_status="in_scope", rationale="auto3",
    )
    # 人工先判定 in_scope = fail (即使 scope_status=in_scope, 人工结论优先)
    record_gate_result(
        db, hyp["hypothesis_id"], "in_scope", "fail",
        evidence="human reviewer overrides", verified_by="human-1",
    )
    db.conn.commit()

    auto_enqueue_validations(db, run_id)
    processed = process_validation_queue(db)
    assert processed["confirmed"] == 1

    gates = {
        g["gate_name"]: g["status"]
        for g in db.fetch_all(
            "SELECT gate_name, status FROM finding_validation_gates WHERE hypothesis_id=?",
            (hyp["hypothesis_id"],),
        )
    }
    assert gates.get("in_scope") == "fail", "人工判定结果不得被自动门覆盖"
    hyp_row = db.fetch_one(
        "SELECT validation_status FROM finding_hypotheses WHERE hypothesis_id=?",
        (hyp["hypothesis_id"],),
    )
    assert hyp_row["validation_status"] == "negative_knowledge"
    print("  ✅ G1: 机器门不覆盖人工判定")


if __name__ == "__main__":
    import tempfile, os
    d = tempfile.mkdtemp()
    db = SwarmDB(os.path.join(d, "t.db"))
    db.init()
    rid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO swarm_runs (run_id, swarm_name, intent, target_type, target_id, status) VALUES (?, 't','recon','webapp','x','running')",
        (rid,),
    )
    db.conn.commit()
    for fn in (
        test_g3_inconclusive_written_as_inconclusive,
        test_g3_inconclusive_not_requeued,
        test_g3_schema_rebuild_and_dirty_cleanup,
        test_g3_migration004_check_contains_inconclusive,
        test_g2_high_entry_requires_replay,
        test_g2_low_entry_keeps_heuristic,
        test_g1_auto_gates_advance_hypothesis,
        test_g1_auto_gates_skip_human_decided,
    ):
        fn(db, rid)
        db.conn.execute("DELETE FROM validation_queue; DELETE FROM knowledge_entries; DELETE FROM knowledge_lineage; DELETE FROM finding_hypotheses; DELETE FROM finding_validation_gates; DELETE FROM negative_knowledge; DELETE FROM counter_examples;")
        db.conn.commit()
    print("ALL MANUAL CHECKS PASSED")
