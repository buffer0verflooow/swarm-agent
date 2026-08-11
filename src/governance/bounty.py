"""
Bounty Knowledge Loop — hypothesis gates + negative knowledge + ROI ranking.

Generic DIKW entries answer "what do we know?".  Bug bounty work also needs a
workflow answer: "is this candidate reportable?".  This module keeps that
workflow as a thin layer over knowledge_entries so existing capture/retrieval
semantics stay intact.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_GATES = (
    "poc_exists",
    "clean_repro",
    "impactful",
    "low_priv_reachable",
    "in_scope",
    "deduplicated",
)

PASSING_GATE_STATUSES = {"pass", "not_applicable"}
FAIL_REASON_BY_GATE = {
    "poc_exists": "not_reproducible",
    "clean_repro": "not_reproducible",
    "impactful": "no_security_impact",
    "low_priv_reachable": "privilege_unreachable",
    "in_scope": "out_of_scope",
    "deduplicated": "duplicate",
}


def _roi_score(expected_payout: float, estimated_hours: float, competition_factor: float) -> float:
    hours = max(float(estimated_hours or 0), 1.0)
    return round((float(expected_payout or 0) / hours) * float(competition_factor or 1.0), 4)


def _parse_trust_vector(value: Any) -> Dict[str, float]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else (value or {})
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    return {
        "logic_soundness": float(parsed.get("logic_soundness", 0.6)),
        "base_confidence": float(parsed.get("base_confidence", 0.6)),
        "cross_validation": float(parsed.get("cross_validation", 0.0)),
    }


def _update_entry_trust(db, knowledge_id: str, base_delta: float, cross_delta: float = 0.0) -> None:
    row = db.fetch_one("SELECT trust_vector FROM knowledge_entries WHERE id=?", (knowledge_id,))
    if not row:
        return
    tv = _parse_trust_vector(row["trust_vector"])
    tv["base_confidence"] = max(0.0, min(1.0, tv["base_confidence"] + base_delta))
    tv["cross_validation"] = max(0.0, min(1.0, tv["cross_validation"] + cross_delta))
    db.execute(
        "UPDATE knowledge_entries SET trust_vector=?, updated_at=datetime('now') WHERE id=?",
        (json.dumps(tv), knowledge_id),
    )


def _row_to_dict(row) -> Dict[str, Any]:
    return dict(row) if row else {}


def _get_hypothesis(db, hypothesis_id: str) -> Dict[str, Any]:
    row = db.fetch_one("SELECT * FROM finding_hypotheses WHERE hypothesis_id=?", (hypothesis_id,))
    if not row:
        return {}
    result = dict(row)
    result["gates"] = [
        dict(g)
        for g in db.fetch_all(
            "SELECT gate_name, status, evidence, verified_by, verified_at "
            "FROM finding_validation_gates WHERE hypothesis_id=? ORDER BY gate_name",
            (hypothesis_id,),
        )
    ]
    return result


def create_finding_hypothesis(
    db,
    knowledge_id: str,
    *,
    target_id: str = "",
    program: str = "",
    vulnerability_class: str = "",
    severity: str = "unknown",
    scope_status: str = "unknown",
    reachability: str = "unknown",
    expected_payout: float = 0.0,
    estimated_hours: float = 0.0,
    competition_factor: float = 1.0,
    rationale: str = "",
    created_by: str = "bounty-loop",
    gates: Iterable[str] = DEFAULT_GATES,
) -> Dict[str, Any]:
    """Create or update a reportability hypothesis for one knowledge entry."""
    entry = db.fetch_one(
        "SELECT id, source_run_id FROM knowledge_entries WHERE id=?",
        (knowledge_id,),
    )
    if not entry:
        raise ValueError(f"knowledge entry not found: {knowledge_id}")

    roi = _roi_score(expected_payout, estimated_hours, competition_factor)
    existing = db.fetch_one(
        "SELECT hypothesis_id FROM finding_hypotheses WHERE knowledge_id=?",
        (knowledge_id,),
    )

    if existing:
        hypothesis_id = existing["hypothesis_id"]
        db.execute(
            """UPDATE finding_hypotheses
               SET target_id=COALESCE(NULLIF(?, ''), target_id),
                   program=COALESCE(NULLIF(?, ''), program),
                   vulnerability_class=COALESCE(NULLIF(?, ''), vulnerability_class),
                   severity=?,
                   scope_status=?,
                   reachability=?,
                   expected_payout=?,
                   estimated_hours=?,
                   competition_factor=?,
                   roi_score=?,
                   rationale=COALESCE(NULLIF(?, ''), rationale),
                   updated_at=datetime('now')
               WHERE hypothesis_id=?""",
            (
                target_id,
                program,
                vulnerability_class,
                severity,
                scope_status,
                reachability,
                float(expected_payout or 0),
                float(estimated_hours or 0),
                float(competition_factor or 1.0),
                roi,
                rationale,
                hypothesis_id,
            ),
        )
    else:
        hypothesis_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO finding_hypotheses
               (hypothesis_id, knowledge_id, run_id, target_id, program,
                vulnerability_class, severity, scope_status, reachability,
                expected_payout, estimated_hours, competition_factor, roi_score,
                rationale, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hypothesis_id,
                knowledge_id,
                entry["source_run_id"],
                target_id,
                program,
                vulnerability_class,
                severity,
                scope_status,
                reachability,
                float(expected_payout or 0),
                float(estimated_hours or 0),
                float(competition_factor or 1.0),
                roi,
                rationale,
                created_by,
            ),
        )

    for gate_name in gates:
        db.execute(
            """INSERT OR IGNORE INTO finding_validation_gates
               (gate_id, hypothesis_id, gate_name)
               VALUES (?, ?, ?)""",
            (str(uuid.uuid4()), hypothesis_id, gate_name),
        )

    db.conn.commit()
    return _get_hypothesis(db, hypothesis_id)


def seed_finding_hypotheses_from_vulnerabilities(
    db,
    run_id: Optional[str] = None,
    *,
    created_by: str = "bounty-loop",
    limit: int = 100,
) -> Dict[str, Any]:
    """Treat active vulnerability entries as untrusted reportability hypotheses."""
    run_clause = "AND ke.source_run_id = ?" if run_id else ""
    params: List[Any] = []
    if run_id:
        params.append(run_id)
    params.append(limit)

    rows = db.fetch_all(
        f"""SELECT ke.id
            FROM knowledge_entries ke
            LEFT JOIN finding_hypotheses fh ON fh.knowledge_id = ke.id
            WHERE ke.status = 'active'
              AND ke.knowledge_type = 'vulnerability'
              AND fh.hypothesis_id IS NULL
              {run_clause}
            ORDER BY ke.created_at DESC
            LIMIT ?""",
        tuple(params),
    )

    created = []
    for row in rows:
        hypothesis = create_finding_hypothesis(db, row["id"], created_by=created_by)
        created.append(hypothesis["hypothesis_id"])

    return {"created": len(created), "hypothesis_ids": created}


def auto_apply_machine_gates(db, hypothesis_id: str, *, verified_by: str = "auto-machine-gate") -> Dict[str, Any]:
    """G1 (2026-08-11 修复): 对已 confirmed 的假设自动判定机器可判门。

    机器可判门:
    - in_scope: 按 finding_hypotheses.scope_status 判定 (in_scope→pass / out_of_scope→fail)
    - deduplicated: 按 knowledge_entries.content_hash 查库内重复条目
    其余门 (poc_exists/clean_repro/impactful/low_priv_reachable) 标记 blocked,
    由人工或独立验证 agent 经 record_gate_result 补证据后推进。

    只覆盖仍为 pending 的门, 不覆盖人工/独立 agent 已判定结果。
    返回 evaluate_hypothesis_gates 的聚合结果 (validating/negative_knowledge/validated)。
    """
    hypothesis = db.fetch_one(
        "SELECT * FROM finding_hypotheses WHERE hypothesis_id=?", (hypothesis_id,)
    )
    if not hypothesis:
        raise ValueError(f"hypothesis not found: {hypothesis_id}")

    def _record_if_pending(gate_name: str, status: str, evidence: str) -> None:
        gate = db.fetch_one(
            "SELECT status FROM finding_validation_gates WHERE hypothesis_id=? AND gate_name=?",
            (hypothesis_id, gate_name),
        )
        if gate and gate["status"] == "pending":
            record_gate_result(
                db, hypothesis_id, gate_name, status,
                evidence=evidence, verified_by=verified_by,
            )

    # --- in_scope: 机器可判 (scope_status 已记录时) ---
    scope = hypothesis["scope_status"] or "unknown"
    if scope == "in_scope":
        _record_if_pending("in_scope", "pass", "scope_status=in_scope")
    elif scope == "out_of_scope":
        _record_if_pending("in_scope", "fail", "scope_status=out_of_scope")
    else:
        _record_if_pending("in_scope", "blocked", "scope_status 未知, 需人工确认")

    # --- deduplicated: 机器可判 (同 content_hash 的 active 条目) ---
    entry = db.fetch_one(
        "SELECT content_hash FROM knowledge_entries WHERE id=?",
        (hypothesis["knowledge_id"],),
    )
    dup_count = 0
    if entry and entry["content_hash"]:
        dup_count = db.fetch_one(
            """SELECT COUNT(*) AS c FROM knowledge_entries
               WHERE content_hash=? AND id!=? AND status='active'""",
            (entry["content_hash"], hypothesis["knowledge_id"]),
        )["c"]
    if dup_count > 0:
        _record_if_pending(
            "deduplicated", "fail",
            f"库内发现 {dup_count} 条相同 content_hash 的重复条目",
        )
    else:
        _record_if_pending("deduplicated", "pass", "库内无 content_hash 重复")

    # --- 其余门: 需人工/独立验证 agent 补证据 ---
    for gate_name in ("poc_exists", "clean_repro", "impactful", "low_priv_reachable"):
        _record_if_pending(gate_name, "blocked", "需人工或独立验证 agent 补证据")

    return evaluate_hypothesis_gates(db, hypothesis_id)


def record_gate_result(
    db,
    hypothesis_id: str,
    gate_name: str,
    status: str,
    *,
    evidence: str = "",
    verified_by: str = "system",
) -> Dict[str, Any]:
    """Record evidence for one validation gate and refresh aggregate status."""
    if gate_name not in DEFAULT_GATES:
        raise ValueError(f"unknown gate: {gate_name}")
    if status not in {"pending", "pass", "fail", "blocked", "not_applicable"}:
        raise ValueError(f"invalid gate status: {status}")

    gate = db.fetch_one(
        "SELECT gate_id FROM finding_validation_gates WHERE hypothesis_id=? AND gate_name=?",
        (hypothesis_id, gate_name),
    )
    if not gate:
        raise ValueError(f"gate not found: {hypothesis_id}:{gate_name}")

    db.execute(
        """UPDATE finding_validation_gates
           SET status=?, evidence=?, verified_by=?, verified_at=datetime('now'),
               updated_at=datetime('now')
           WHERE hypothesis_id=? AND gate_name=?""",
        (status, evidence, verified_by, hypothesis_id, gate_name),
    )
    return evaluate_hypothesis_gates(db, hypothesis_id)


def evaluate_hypothesis_gates(db, hypothesis_id: str) -> Dict[str, Any]:
    """Aggregate gate statuses into hypothesis status and side effects."""
    hypothesis = db.fetch_one(
        "SELECT * FROM finding_hypotheses WHERE hypothesis_id=?",
        (hypothesis_id,),
    )
    if not hypothesis:
        raise ValueError(f"hypothesis not found: {hypothesis_id}")

    gates = db.fetch_all(
        "SELECT gate_name, status, evidence, verified_by FROM finding_validation_gates WHERE hypothesis_id=?",
        (hypothesis_id,),
    )
    gate_status = {g["gate_name"]: g["status"] for g in gates}
    failed = [g for g in gates if g["status"] == "fail"]
    pending = [g for g in gates if g["status"] == "pending"]
    blocked = [g for g in gates if g["status"] == "blocked"]

    if failed:
        status = "negative_knowledge"
        for gate in failed:
            record_negative_knowledge(
                db,
                knowledge_id=hypothesis["knowledge_id"],
                hypothesis_id=hypothesis_id,
                run_id=hypothesis["run_id"],
                target_id=hypothesis["target_id"],
                program=hypothesis["program"],
                reason_type=FAIL_REASON_BY_GATE.get(gate["gate_name"], "false_positive"),
                details=gate["evidence"] or f"Gate failed: {gate['gate_name']}",
                created_by=gate["verified_by"] or "bounty-loop",
                commit=False,
            )
        _update_entry_trust(db, hypothesis["knowledge_id"], base_delta=-0.20, cross_delta=-0.10)
    elif pending or blocked:
        status = "validating"
    elif gate_status and all(state in PASSING_GATE_STATUSES for state in gate_status.values()):
        status = "validated"
        _update_entry_trust(db, hypothesis["knowledge_id"], base_delta=0.10, cross_delta=0.20)
        db.execute(
            """UPDATE knowledge_entries
               SET level = CASE WHEN level < 3 THEN 3 ELSE level END,
                   last_validated_at=datetime('now'),
                   validation_count=validation_count + 1,
                   updated_at=datetime('now')
               WHERE id=?""",
            (hypothesis["knowledge_id"],),
        )
    else:
        status = "hypothesis"

    db.execute(
        """UPDATE finding_hypotheses
           SET validation_status=?,
               validated_at=CASE WHEN ? = 'validated' THEN datetime('now') ELSE validated_at END,
               updated_at=datetime('now')
           WHERE hypothesis_id=?""",
        (status, status, hypothesis_id),
    )
    db.conn.commit()

    return {
        "hypothesis_id": hypothesis_id,
        "status": status,
        "gates": gate_status,
        "failed": [g["gate_name"] for g in failed],
        "pending": [g["gate_name"] for g in pending],
        "blocked": [g["gate_name"] for g in blocked],
    }


def record_negative_knowledge(
    db,
    *,
    knowledge_id: Optional[str] = None,
    hypothesis_id: Optional[str] = None,
    run_id: Optional[str] = None,
    target_id: str = "",
    program: str = "",
    reason_type: str = "other",
    details: str,
    created_by: str = "bounty-loop",
    commit: bool = True,
) -> str:
    """Persist a reusable negative result and mirror it into counter_examples."""
    existing = None
    if hypothesis_id:
        existing = db.fetch_one(
            "SELECT negative_id FROM negative_knowledge WHERE hypothesis_id=? AND reason_type=?",
            (hypothesis_id, reason_type),
        )
    if existing:
        return existing["negative_id"]

    negative_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO negative_knowledge
           (negative_id, knowledge_id, hypothesis_id, run_id, target_id, program,
            reason_type, details, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            negative_id,
            knowledge_id,
            hypothesis_id,
            run_id,
            target_id,
            program,
            reason_type,
            details,
            created_by,
        ),
    )

    if knowledge_id:
        db.execute(
            """INSERT INTO counter_examples
               (id, knowledge_id, source_run_id, source_agent, description, evidence, severity)
               VALUES (?, ?, ?, ?, ?, ?, 'moderate')""",
            (
                str(uuid.uuid4()),
                knowledge_id,
                run_id,
                created_by,
                f"Negative knowledge: {reason_type}",
                details,
            ),
        )

    if commit:
        db.conn.commit()
    return negative_id


def rank_hypotheses_by_roi(
    db,
    *,
    statuses: Iterable[str] = ("hypothesis", "validating"),
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Return candidate findings ordered by conservative expected value."""
    status_list = list(statuses)
    if not status_list:
        return []

    placeholders = ",".join("?" * len(status_list))
    rows = db.fetch_all(
        f"""SELECT fh.*, ke.title, ke.knowledge_type, ke.domain, ke.tags
            FROM finding_hypotheses fh
            JOIN knowledge_entries ke ON ke.id = fh.knowledge_id
            WHERE fh.validation_status IN ({placeholders})
            ORDER BY fh.roi_score DESC, fh.expected_payout DESC, fh.updated_at DESC
            LIMIT ?""",
        tuple(status_list + [limit]),
    )
    return [_row_to_dict(row) for row in rows]


def get_negative_knowledge(
    db,
    *,
    target_id: str = "",
    program: str = "",
    reason_type: str = "",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Fetch reusable dead-end knowledge before starting another campaign."""
    conditions = []
    params: List[Any] = []
    if target_id:
        conditions.append("target_id = ?")
        params.append(target_id)
    if program:
        conditions.append("program = ?")
        params.append(program)
    if reason_type:
        conditions.append("reason_type = ?")
        params.append(reason_type)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(limit)
    rows = db.fetch_all(
        f"""SELECT * FROM negative_knowledge
            {where}
            ORDER BY created_at DESC
            LIMIT ?""",
        tuple(params),
    )
    return [_row_to_dict(row) for row in rows]


def bounty_loop_summary(db) -> Dict[str, Any]:
    """Compact dashboard data for the hypothesis/negative-knowledge loop."""
    by_status = {
        row["validation_status"]: row["cnt"]
        for row in db.fetch_all(
            "SELECT validation_status, COUNT(*) AS cnt FROM finding_hypotheses GROUP BY validation_status"
        )
    }
    by_negative_reason = {
        row["reason_type"]: row["cnt"]
        for row in db.fetch_all(
            "SELECT reason_type, COUNT(*) AS cnt FROM negative_knowledge GROUP BY reason_type"
        )
    }
    top_roi = rank_hypotheses_by_roi(db, limit=5)
    return {
        "hypotheses_by_status": by_status,
        "negative_by_reason": by_negative_reason,
        "top_roi": top_roi,
    }
