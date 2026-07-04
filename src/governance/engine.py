"""
Swarm Knowledge Governance — SQLite 版

DIKW 提升 + 反例衰减 + 交叉验证 + 聚类去重
单 SQLite 文件 + Python NetworkX 做图分析
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

_log = logging.getLogger("swarm_knowledge.governance")

# ── 提升阈值 ──

PROMOTION_THRESHOLDS = {
    2: {"min_corroborating": 1, "min_confidence": 0.60},
    3: {"min_corroborating": 2, "min_confidence": 0.75},
    4: {"min_corroborating": 3, "min_confidence": 0.85},
}

COUNTER_THRESHOLD = 5


# ============================================================================
# DIKW Promotion
# ============================================================================

def compute_trust_score(tv_json: str) -> float:
    """从 trust_vector JSON 计算复合置信度"""
    try:
        tv = json.loads(tv_json) if isinstance(tv_json, str) else tv_json
    except (json.JSONDecodeError, TypeError):
        tv = {}
    base = float(tv.get("base_confidence", 0.6))
    logic = float(tv.get("logic_soundness", 0.7))
    cross = float(tv.get("cross_validation", 0.0))
    return 0.4 * base + 0.3 * logic + 0.3 * cross


def run_promotion_cycle(db) -> Dict[str, Any]:
    """扫描知识条目，自动提升满足条件的条目"""
    rows = db.fetch_all(
        "SELECT id, level, trust_vector, status FROM knowledge_entries "
        "WHERE status = 'active' ORDER BY level DESC, created_at DESC LIMIT 500"
    )

    promoted = []
    for row in rows:
        entry_id = row["id"]
        current_level = int(row["level"])

        # 统计 corroborating sources
        corr = db.fetch_one(
            "SELECT COUNT(DISTINCT source_type) AS cnt FROM knowledge_lineage "
            "WHERE knowledge_id = ? AND confidence_contribution > 0.5",
            (entry_id,),
        )
        count = corr["cnt"] if corr else 0

        # 判断是否满足提升条件
        for target_level in range(current_level + 1, 5):
            threshold = PROMOTION_THRESHOLDS.get(target_level, {})
            trust = compute_trust_score(row["trust_vector"] or "{}")
            if count >= threshold.get("min_corroborating", 99) and trust >= threshold.get("min_confidence", 0.99):
                db.execute(
                    "UPDATE knowledge_entries SET level = ?, promoted_at = datetime('now'), promoted_by = 'governance' "
                    "WHERE id = ?",
                    (target_level, entry_id),
                )
                db.execute(
                    "INSERT INTO knowledge_promotions (knowledge_id, from_level, to_level, promoted_by, reason) "
                    "VALUES (?, ?, ?, 'governance', 'Auto: trust=%.2f, corroborating=%d')" % (trust, count),
                    (entry_id, current_level, target_level),
                )
                promoted.append({
                    "id": entry_id[:8], "from": current_level, "to": target_level,
                    "trust": round(trust, 2), "corroborating": count,
                })
                break

    db.conn.commit()
    _log.info("promotion_cycle: %d entries promoted", len(promoted))
    return {"promoted": len(promoted), "details": promoted}


# ============================================================================
# Pheromone Evaporation (Time-based Decay)
# ============================================================================

# 信息素衰减参数
PHEROMONE_DECAY_RATE = 0.95       # 每个衰减周期乘以 0.95
PHEROMONE_DECAY_INTERVAL_HOURS = 6  # 每 6 小时衰减一次
PHEROMONE_MIN_THRESHOLD = 0.1     # 低于此值的条目标记为 stale
PHEROMONE_VALIDATION_BOOST = 0.3  # 每次验证增加的 pheromone


def run_pheromone_decay(db, interval_hours: int = PHEROMONE_DECAY_INTERVAL_HOURS) -> Dict[str, Any]:
    """
    信息素蒸发 — ACOFuzz 模式的时间驱动衰减。
    
    长时间未被验证的知识条目 pheromone 逐渐降低，
    低于阈值的自动标记为 stale，让 agent 去别处探索。
    
    每次验证（cross_validate 或 去重确认）会重置 pheromone 为 1.0。
    """
    # 找到需要衰减的条目: 上次衰减时间超过 interval
    rows = db.fetch_all(
        """SELECT id, pheromone, last_validated_at, level
           FROM knowledge_entries
           WHERE status = 'active' AND pheromone IS NOT NULL
           ORDER BY level DESC LIMIT 1000""",
    )

    decayed = []
    stale_marked = []
    import time as _time
    now = _time.time()

    for row in rows:
        # 计算 last_validated_at 到现在的小时数
        lv = row["last_validated_at"]
        if lv:
            from datetime import datetime
            try:
                lv_dt = datetime.fromisoformat(lv.replace("Z", ""))
                hours_elapsed = (now - lv_dt.timestamp()) / 3600
            except (ValueError, TypeError):
                hours_elapsed = 0
        else:
            hours_elapsed = 0

        if hours_elapsed < interval_hours:
            continue

        # 指数衰减: pheromone *= decay_rate ^ (elapsed_intervals)
        intervals = int(hours_elapsed / interval_hours)
        new_pheromone = row["pheromone"] * (PHEROMONE_DECAY_RATE ** intervals)

        if new_pheromone < PHEROMONE_MIN_THRESHOLD:
            # 低于阈值 → 标记为 stale
            db.execute(
                "UPDATE knowledge_entries SET pheromone = ?, status = 'stale', updated_at = datetime('now') WHERE id = ?",
                (round(new_pheromone, 4), row["id"]),
            )
            stale_marked.append({"id": row["id"][:8], "level": row["level"], "pheromone": round(new_pheromone, 4)})
        elif new_pheromone < row["pheromone"]:
            db.execute(
                "UPDATE knowledge_entries SET pheromone = ?, updated_at = datetime('now') WHERE id = ?",
                (round(new_pheromone, 4), row["id"]),
            )
            decayed.append({"id": row["id"][:8], "old": row["pheromone"], "new": round(new_pheromone, 4)})

    db.conn.commit()
    _log.info("pheromone_decay: %d decayed, %d marked stale", len(decayed), len(stale_marked))
    return {"decayed": decayed, "stale_marked": stale_marked}


def boost_pheromone(db, entry_id: str, boost: float = PHEROMONE_VALIDATION_BOOST) -> None:
    """验证或确认一条知识时，增加其 pheromone（最高 1.0）。"""
    db.execute(
        "UPDATE knowledge_entries SET pheromone = MIN(1.0, COALESCE(pheromone, 0.5) + ?), last_validated_at = datetime('now'), validation_count = validation_count + 1 WHERE id = ?",
        (boost, entry_id),
    )
    db.conn.commit()


# ============================================================================
# Strategy Auto-Distillation
# ============================================================================

def auto_distill_strategies(db, min_success_rate: float = 0.6, min_samples: int = 3) -> Dict[str, Any]:
    """
    从成功的 task 执行模式中自动蒸馏策略，写入 swarm_strategies 表。
    
    分析 agent_tasks 中 (task_type, focus_params) 组合的历史成功率，
    将高成功率的模式提取为可复用的策略。
    """
    # 分析成功的任务模式
    rows = db.fetch_all(
        """SELECT task_type, focus_params,
                  COUNT(*) AS total,
                  SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS successes,
                  AVG(token_cost) AS avg_tokens
           FROM agent_tasks
           WHERE status IN ('completed', 'failed')
           GROUP BY task_type, focus_params
           HAVING COUNT(*) >= ?
           ORDER BY successes DESC LIMIT 50""",
        (min_samples,),
    )

    distilled = []
    for row in rows:
        success_rate = row["successes"] / row["total"] if row["total"] > 0 else 0
        if success_rate < min_success_rate:
            continue

        # 从 focus_params 提取策略描述
        try:
            fp = json.loads(row["focus_params"]) if isinstance(row["focus_params"], str) else (row["focus_params"] or {})
        except (json.JSONDecodeError, TypeError):
            fp = {}

        tool = fp.get("tool", "")
        technique = fp.get("technique", "")
        strategy_name = f"auto:{row['task_type']}:{tool or technique}"

        # 检查是否已存在
        existing = db.fetch_one("SELECT strategy_id FROM swarm_strategies WHERE strategy_name = ?", (strategy_name,))
        if existing:
            # 更新统计
            db.execute(
                "UPDATE swarm_strategies SET use_count = ?, success_count = ?, avg_duration_ms = ?, updated_at = datetime('now') WHERE strategy_name = ?",
                (row["total"], row["successes"], int(row["avg_tokens"] or 0), strategy_name),
            )
            continue

        strategy_id = str(uuid.uuid4())
        strategy_body = json.dumps({
            "task_type": row["task_type"],
            "tool": tool,
            "technique": technique,
            "params": fp,
            "success_rate": round(success_rate, 2),
            "sample_count": row["total"],
        })

        db.execute(
            """INSERT INTO swarm_strategies
               (strategy_id, strategy_name, description, strategy_type, strategy_body,
                trigger_intent, trigger_target_type, trigger_complexity,
                use_count, success_count, avg_duration_ms, is_active, priority,
                auto_distilled, distilled_from_runs)
               VALUES (?, ?, ?, 'task_decomposition', ?, ?, ?, 'medium',
                ?, ?, ?, 1, ?, 1, '[]')""",
            (
                strategy_id, strategy_name,
                f"Auto-distilled from {row['total']} executions ({success_rate:.0%} success)",
                strategy_body,
                fp.get("intent", row["task_type"]),
                fp.get("target_type", "unknown"),
                row["total"], row["successes"], int(row["avg_tokens"] or 0),
                int(success_rate * 100),
            ),
        )
        distilled.append({"name": strategy_name, "success_rate": round(success_rate, 2), "samples": row["total"]})

    db.conn.commit()
    _log.info("auto_distill: %d strategies distilled", len(distilled))
    return {"distilled": distilled}

def check_and_decay(db, threshold: int = COUNTER_THRESHOLD) -> Dict[str, Any]:
    """反例驱动的知识衰减"""

    # 衰减 distilled_rules
    rules = db.fetch_all(
        "SELECT id, rule_name, source_knowledge_ids, counter_example_count "
        "FROM distilled_rules WHERE is_active = 1"
    )
    decayed_rules = []
    for rule in rules:
        rule_id = rule["id"]
        source_ids = json.loads(rule["source_knowledge_ids"] or "[]")

        # 统计关联的反例
        if source_ids:
            placeholders = ",".join("?" * len(source_ids))
            ce = db.fetch_one(
                f"SELECT COUNT(*) AS cnt FROM counter_examples WHERE knowledge_id IN ({placeholders})",
                tuple(source_ids),
            )
            total = (rule["counter_example_count"] or 0) + (ce["cnt"] if ce else 0)
        else:
            total = rule["counter_example_count"] or 0

        if total >= threshold:
            db.execute(
                "UPDATE distilled_rules SET is_active = 0, counter_example_count = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (total, rule_id),
            )
            decayed_rules.append({"rule_id": rule_id[:8], "name": rule["rule_name"], "ce_count": total})

    # 衰减 knowledge_entries
    entries = db.fetch_all("SELECT id, title, level FROM knowledge_entries WHERE level >= 3 AND status = 'active'")
    decayed_entries = []
    for entry in entries:
        ce = db.fetch_one("SELECT COUNT(*) AS cnt FROM counter_examples WHERE knowledge_id = ?", (entry["id"],))
        if (ce["cnt"] if ce else 0) >= threshold:
            db.execute(
                "UPDATE knowledge_entries SET status = 'stale', updated_at = datetime('now') WHERE id = ?",
                (entry["id"],),
            )
            decayed_entries.append({
                "id": entry["id"][:8],
                "title": entry["title"] or "",
                "ce_count": ce["cnt"],
            })

    db.conn.commit()
    _log.info("decay: %d rules + %d entries", len(decayed_rules), len(decayed_entries))
    return {"decayed_rules": decayed_rules, "decayed_entries": decayed_entries}


# ============================================================================
# Cross-Validation
# ============================================================================

def cross_validate(db, entry_id: str, source_agent: str, verdict: str, evidence: str = "") -> Dict[str, Any]:
    """Agent 对一条知识的交叉验证"""
    row = db.fetch_one("SELECT trust_vector FROM knowledge_entries WHERE id = ?", (entry_id,))
    if not row:
        return {"error": "entry not found"}

    tv = json.loads(row["trust_vector"] or "{}")
    is_refute = verdict == "refute"

    # 更新 trust_vector
    cv = float(tv.get("cross_validation", 0))
    bc = float(tv.get("base_confidence", 0.6))
    tv["cross_validation"] = min(1.0, cv + (-0.15 if is_refute else 0.10))
    tv["base_confidence"] = max(0.0, bc + (-0.10 if is_refute else 0.05))

    db.execute(
        "UPDATE knowledge_entries SET trust_vector = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(tv), entry_id),
    )

    if is_refute:
        db.execute(
            "INSERT INTO counter_examples (id, knowledge_id, source_agent, description, evidence) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), entry_id, source_agent, f"Refuted by {source_agent}", evidence),
        )

    db.conn.commit()
    return {"entry_id": entry_id[:8], "verdict": verdict, "new_tv": tv}


# ============================================================================
# Clustering (SQLite + NetworkX)
# ============================================================================

WEAK_LINK_THRESHOLD = 0.82
MAX_WEAK_LINKS = 15


def build_similarity_graph(
    db,
    threshold: float = WEAK_LINK_THRESHOLD,
    limit: int = 500,
    use_ontology: bool = True,
) -> Dict[str, Any]:
    """
    构建知识相似度图。

    不使用 embedding (SQLite 没有 sqlite-vec 时用 tag overlap + content length 模拟)。
    实际使用时应该集成 embedding 模型。
    
    如果 use_ontology=True，先用 ontology 关系扩展 tags:
    - 子概念 tag 自动加入 (sql_injection → injection)
    - 父概念 tag 自动加入 (nmap → port_scan via implements 关系)
    这解决了 "sqli" vs "injection" vs "sql_injection" 的语义鸿沟问题。
    """
    rows = db.fetch_all(
        "SELECT id, tags, content FROM knowledge_entries WHERE status = 'active' AND level >= 1 ORDER BY created_at LIMIT ?",
        (limit,),
    )
    if len(rows) < 2:
        return {"entries": len(rows), "links": 0}

    # Build ontology tag expansion map
    tag_expansion = {}
    if use_ontology:
        tag_expansion = _build_ontology_tag_expansion(db)

    # Tag-based Jaccard similarity with ontology expansion
    entries = []
    for r in rows:
        try:
            tags = set(json.loads(r["tags"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            tags = set()
        # Expand tags using ontology
        expanded = set(tags)
        for tag in tags:
            if tag in tag_expansion:
                expanded |= tag_expansion[tag]
        entries.append({"id": r["id"], "tags": expanded, "original_tags": tags, "len": len(r["content"] or "")})

    links = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]

            # Jaccard similarity on expanded tags
            union = len(a["tags"] | b["tags"])
            if union == 0:
                continue
            jaccard = len(a["tags"] & b["tags"]) / union

            if jaccard >= threshold:
                links.append((a["id"], b["id"], jaccard))

    _log.info("similarity_graph: %d entries, %d links (ontology=%s)", len(entries), len(links), use_ontology)
    return {"entries": len(entries), "links": len(links), "link_pairs": links}


def _build_ontology_tag_expansion(db) -> Dict[str, set]:
    """构建 ontology tag 扩展映射。
    
    返回 {tag: {扩展后的 tag 集合}}，包括:
    - 父子关系: injection → {sqli, sql_injection, xss, ...}
    - implements 关系: nmap → {port_scan, ...}
    - 特殊化/泛化: sql_injection → {injection}
    """
    expansion = {}
    
    # 获取所有概念名 → concept_id 映射
    concepts = db.fetch_all("SELECT concept_id, concept_name FROM ontology_concepts")
    name_to_id = {r["concept_name"]: r["concept_id"] for r in concepts}
    
    # 获取父子关系
    parent_relations = db.fetch_all(
        """SELECT c1.concept_name AS child, c2.concept_name AS parent
           FROM ontology_concepts c1
           JOIN ontology_concepts c2 ON c1.parent_concept_id = c2.concept_id""",
    )
    for r in parent_relations:
        child = r["child"]
        parent = r["parent"]
        if child not in expansion:
            expansion[child] = set()
        expansion[child].add(parent)
        if parent not in expansion:
            expansion[parent] = set()
        expansion[parent].add(child)
    
    # 获取 implements/uses/produces 关系
    impl_relations = db.fetch_all(
        """SELECT c1.concept_name AS from_name, c2.concept_name AS to_name, r.relation_type
           FROM ontology_relations r
           JOIN ontology_concepts c1 ON r.from_concept_id = c1.concept_id
           JOIN ontology_concepts c2 ON r.to_concept_id = c2.concept_id
           WHERE r.relation_type IN ('implements', 'uses', 'produces', 'specializes', 'generalizes')""",
    )
    for r in impl_relations:
        from_name = r["from_name"]
        to_name = r["to_name"]
        if from_name not in expansion:
            expansion[from_name] = set()
        if to_name not in expansion:
            expansion[to_name] = set()
        expansion[from_name].add(to_name)
        expansion[to_name].add(from_name)
    
    return expansion


def detect_communities_louvain(db, links: List[Tuple[str, str, float]]) -> Dict[str, Any]:
    """Louvain 社区检测（纯 Python NetworkX）"""
    from networkx import Graph
    from networkx.algorithms.community import louvain_communities

    if len(links) < 2:
        return {"communities": 0, "nodes": 0}

    G = Graph()
    for src, tgt, weight in links:
        G.add_edge(src, tgt, weight=weight)

    communities = louvain_communities(G, weight="weight", seed=42)
    community_list = [list(c) for c in communities]

    # Write cluster_id back to DB
    updated = 0
    for i, community in enumerate(community_list):
        if len(community) < 2:
            continue
        cluster_id = str(uuid.uuid4())
        centroid = max(community, key=lambda n: G.degree(n))

        for node_id in community:
            is_centroid = 1 if node_id == centroid else 0
            db.execute(
                "UPDATE knowledge_entries SET cluster_id = ?, is_cluster_centroid = ?, cluster_updated_at = datetime('now') "
                "WHERE id = ?",
                (cluster_id, is_centroid, node_id),
            )
            updated += 1

    db.conn.commit()
    _log.info("detect_communities: %d communities, %d nodes", len(community_list), updated)
    return {"communities": len(community_list), "nodes_assigned": updated, "total_nodes": G.number_of_nodes()}


def run_full_clustering(db) -> Dict[str, Any]:
    """完整聚类流程"""
    t0 = time.time()
    result = {"phase": "clustering", "started_at": t0}

    graph = build_similarity_graph(db)
    result["graph"] = graph

    if graph.get("link_pairs"):
        communities = detect_communities_louvain(db, graph["link_pairs"])
        result["communities"] = communities

    result["elapsed_sec"] = round(time.time() - t0, 1)
    return result
