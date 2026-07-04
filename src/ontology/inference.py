"""
Swarm Ontology Inference — SQLite 版

本体推理引擎: 概念发现、关系推理、漂移检测、合并建议
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

_log = logging.getLogger("swarm_knowledge.ontology")


# ============================================================================
# Concept Discovery
# ============================================================================

def discover_concepts_from_tasks(
    db,
    run_id: Optional[str] = None,
    min_occurrence: int = 3,
) -> List[Dict[str, Any]]:
    """从任务执行记录中发现新概念"""
    conditions = ["at.status IN ('completed', 'failed')"]
    params: List[Any] = []

    if run_id:
        conditions.append("at.run_id = ?")
        params.append(run_id)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT
            at.task_type,
            at.focus_params,
            COUNT(*) AS cnt,
            AVG(CASE WHEN at.status = 'completed' THEN 1.0 ELSE 0.0 END) AS success_rate
        FROM agent_tasks at
        WHERE {where}
        GROUP BY at.task_type
        HAVING COUNT(*) >= ?
    """
    params.append(min_occurrence)

    rows = db.fetch_all(sql, tuple(params))
    discovered = []

    for row in rows:
        task_type = row["task_type"]

        # Check if concept already exists
        existing = db.fetch_one("SELECT concept_id FROM ontology_concepts WHERE concept_name = ?", (task_type,))
        if existing:
            continue

        # Try to extract tool/technique from focus_params
        try:
            fp = json.loads(row["focus_params"]) if isinstance(row["focus_params"], str) else (row["focus_params"] or {})
        except (json.JSONDecodeError, TypeError):
            fp = {}

        concept_name = fp.get("tool") or fp.get("technique") or task_type
        concept_type = "tool" if fp.get("tool") else ("technique" if fp.get("technique") else "task_type")

        discovered.append({
            "concept_name": concept_name,
            "concept_type": concept_type,
            "occurrence_count": int(row["cnt"]),
            "success_rate": float(row["success_rate"] or 0),
            "source": "agent_discovered",
        })

    _log.info("discover_concepts: %d candidates", len(discovered))
    return discovered


def register_concepts(db, concepts: List[Dict[str, Any]]) -> int:
    """将发现的概念注册到本体"""
    registered = 0
    for c in concepts:
        existing = db.fetch_one("SELECT concept_id FROM ontology_concepts WHERE concept_name = ?", (c["concept_name"],))
        if existing:
            continue
        cid = str(uuid.uuid4())
        db.execute(
            """INSERT INTO ontology_concepts (concept_id, concept_name, concept_type, description, source, properties)
               VALUES (?, ?, ?, ?, 'agent_discovered', '{"auto_discovered": true}')""",
            (
                cid, c["concept_name"], c["concept_type"],
                f"Discovered from {c['occurrence_count']} tasks (success_rate={c['success_rate']:.2f})",
            ),
        )
        registered += 1
    db.conn.commit()
    return registered


# ============================================================================
# Relation Inference
# ============================================================================

def infer_transitive_relations(db, relation_type: str = "depends_on") -> List[Dict[str, Any]]:
    """传递闭包: A→B + B→C → A→C"""
    rows = db.fetch_all(
        """SELECT DISTINCT r1.from_concept_id AS a, r2.to_concept_id AS c
           FROM ontology_relations r1
           JOIN ontology_relations r2 ON r1.to_concept_id = r2.from_concept_id
           WHERE r1.relation_type = ? AND r2.relation_type = ?""",
        (relation_type, relation_type),
    )

    inferred = []
    for row in rows:
        a_id = row["a"]
        c_id = row["c"]
        if a_id == c_id:
            continue

        existing = db.fetch_one(
            "SELECT 1 FROM ontology_relations WHERE from_concept_id = ? AND to_concept_id = ? AND relation_type = ?",
            (a_id, c_id, relation_type),
        )
        if existing:
            continue

        cur = db.execute(
            """INSERT INTO ontology_relations
               (relation_id, from_concept_id, to_concept_id, relation_type, confidence, source)
               VALUES (?, ?, ?, ?, 0.6, 'inferred')""",
            (str(uuid.uuid4()), a_id, c_id, relation_type),
        )
        if cur.rowcount == 1:
            inferred.append({"from": a_id[:8], "to": c_id[:8]})

    db.conn.commit()
    _log.info("infer_transitive: %d new relations", len(inferred))
    return inferred


# ============================================================================
# Merge Suggestions
# ============================================================================

def suggest_merges(db, min_similarity: float = 0.7) -> List[Dict[str, Any]]:
    """基于概念名相似度建议合并"""
    rows = db.fetch_all(
        """SELECT c1.concept_name AS n1, c2.concept_name AS n2,
                  c1.concept_id AS id1, c2.concept_id AS id2
           FROM ontology_concepts c1
           JOIN ontology_concepts c2
             ON c1.concept_type = c2.concept_type
            AND c1.concept_id < c2.concept_id
           WHERE c1.is_abstract = 0 AND c2.is_abstract = 0""",
    )

    suggestions = []
    for row in rows:
        n1, n2 = row["n1"], row["n2"]
        # Simple word overlap similarity
        w1 = set(n1.lower().replace("_", " ").split())
        w2 = set(n2.lower().replace("_", " ").split())
        if not w1 or not w2:
            continue
        sim = len(w1 & w2) / max(len(w1), len(w2))
        if sim >= min_similarity:
            suggestions.append({
                "name_a": n1, "name_b": n2,
                "id_a": row["id1"][:8], "id_b": row["id2"][:8],
                "similarity": round(sim, 2),
            })

    _log.info("suggest_merges: %d candidates", len(suggestions))
    return suggestions


# ============================================================================
# Concept Drift Detection
# ============================================================================

def detect_concept_drift(db, concept_id: str, recent_days: int = 30) -> Optional[Dict[str, Any]]:
    """检测概念使用模式漂移"""
    recent = db.fetch_one(
        """SELECT COUNT(*) AS cnt, AVG(success_rate) AS avg_success
           FROM ontology_instances
           WHERE concept_id = ? AND updated_at > datetime('now', ?)""",
        (concept_id, f"-{recent_days} days"),
    )
    historical = db.fetch_one(
        """SELECT COUNT(*) AS cnt, AVG(success_rate) AS avg_success
           FROM ontology_instances
           WHERE concept_id = ? AND updated_at <= datetime('now', ?)""",
        (concept_id, f"-{recent_days} days"),
    )

    rc = int(recent["cnt"]) if recent else 0
    hc = int(historical["cnt"]) if historical else 0
    if hc == 0:
        return None

    rs = float(recent["avg_success"] or 0)
    hs = float(historical["avg_success"] or 0)
    drift = abs(rs - hs)

    if drift > 0.2:
        return {
            "concept_id": concept_id[:8],
            "drift": round(drift, 2),
            "recent_success": round(rs, 2), "historical_success": round(hs, 2),
            "direction": "improving" if rs > hs else "declining",
        }
    return None


def run_ontology_maintenance(db) -> Dict[str, Any]:
    """本体维护周期"""
    result = {}

    discovered = discover_concepts_from_tasks(db)
    if discovered:
        registered = register_concepts(db, discovered)
        result["discovered"] = len(discovered)
        result["registered"] = registered

    inferred = infer_transitive_relations(db)
    result["relations_inferred"] = len(inferred)

    merges = suggest_merges(db)
    result["merge_suggestions"] = len(merges)

    return result
