"""
Ontology 关系自动发现 — 从知识条目共现提取 concept→concept 关系

种子数据只定义了 3 条关系(nmap→port_scan, nuclei→vuln_scan, sqlmap→sqli)。
本模块从知识库中实际观察到的 tag 共现模式自动发现新关系。

方法:
1. 遍历所有知识条目的 tags
2. 当两个 ontology 概念作为 tag 在同一条知识中共同出现时，记录共现
3. 高频共现 → 推断关系类型并写入 ontology_relations
4. 关系类型推断: 同类型概念共现 → "related_to"; tool+technique → "implements";
   vulnerability+technique → "exploits"; tool+vulnerability → "detects"

用法:
    from src.ontology.discovery import discover_relations_from_cooccurrence
    new_relations = discover_relations_from_cooccurrence(db)
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

_log = logging.getLogger("swarm_knowledge.ontology.discovery")

# 共现阈值
MIN_COOCCURRENCE = 2  # 至少共现 N 次才推断关系
MAX_RELATIONS_PER_RUN = 50  # 每次最多发现 N 条关系


def discover_relations_from_cooccurrence(db) -> Dict[str, Any]:
    """
    从知识条目的 tag 共现模式自动发现 ontology 关系。

    返回:
        {"discovered": [...], "skipped_existing": N}
    """
    # 1. 获取所有概念名 (from ontology_concepts)
    concepts = db.fetch_all(
        "SELECT concept_id, concept_name, concept_type FROM ontology_concepts WHERE is_abstract = 0"
    )
    concept_map = {c["concept_name"]: c for c in concepts}
    name_to_id = {c["concept_name"]: c["concept_id"] for c in concepts}

    if not concept_map:
        return {"discovered": [], "reason": "no_concepts"}

    # 2. 遍历知识条目, 收集 tag 共现
    entries = db.fetch_all(
        "SELECT id, tags, domain, knowledge_type FROM knowledge_entries WHERE status = 'active'"
    )

    co_occurrence = defaultdict(lambda: {"count": 0, "contexts": []})

    for entry in entries:
        try:
            tags = json.loads(entry["tags"]) if isinstance(entry["tags"], str) else (entry["tags"] or [])
        except (json.JSONDecodeError, TypeError):
            tags = []

        # 找出 tags 中属于 ontology 概念的部分
        concept_tags = [t for t in tags if t in name_to_id]
        if len(concept_tags) < 2:
            continue

        # 记录所有两两共现
        for i in range(len(concept_tags)):
            for j in range(i + 1, len(concept_tags)):
                pair = tuple(sorted([concept_tags[i], concept_tags[j]]))
                co_occurrence[pair]["count"] += 1
                if len(co_occurrence[pair]["contexts"]) < 3:
                    co_occurrence[pair]["contexts"].append({
                        "entry_id": entry["id"][:8],
                        "domain": entry["domain"],
                        "ktype": entry["knowledge_type"],
                    })

    # 3. 过滤低频共现，推断关系类型
    discovered = []
    skipped = 0

    for (name_a, name_b), data in sorted(co_occurrence.items(), key=lambda x: -x[1]["count"]):
        if data["count"] < MIN_COOCCURRENCE:
            continue

        # 检查是否已有关系
        concept_a = name_to_id[name_a]
        concept_b = name_to_id[name_b]
        existing = db.fetch_one(
            """SELECT 1 FROM ontology_relations
               WHERE from_concept_id = ? AND to_concept_id = ? AND relation_type = ?
               OR from_concept_id = ? AND to_concept_id = ? AND relation_type = ?""",
            (concept_a, concept_b, "composes", concept_b, concept_a, "composes"),
        )
        if existing:
            skipped += 1
            continue

        # 推断关系类型
        type_a = concept_map[name_a]["concept_type"]
        type_b = concept_map[name_b]["concept_type"]
        relation_type = _infer_relation_type(type_a, type_b)

        if not relation_type:
            continue

        # 决定 from→to 方向
        from_name, to_name, from_id, to_id = _determine_direction(
            name_a, name_b, concept_a, concept_b, type_a, type_b, relation_type
        )

        # 写入关系
        rel_id = str(uuid.uuid4())
        confidence = min(1.0, 0.5 + data["count"] * 0.1)  # 共现越多置信度越高

        db.execute(
            """INSERT OR IGNORE INTO ontology_relations
               (relation_id, from_concept_id, to_concept_id, relation_type,
                weight, confidence, evidence, source, co_occurrence_count, last_observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'co_occurrence', ?, datetime('now'))""",
            (
                rel_id, from_id, to_id, relation_type,
                round(data["count"] / 10, 2), confidence,
                json.dumps({"co_occurrence": data["count"], "contexts": data["contexts"]}),
                data["count"],
            ),
        )

        discovered.append({
            "from": from_name,
            "to": to_name,
            "type": relation_type,
            "co_occurrence": data["count"],
            "confidence": round(confidence, 2),
        })

        if len(discovered) >= MAX_RELATIONS_PER_RUN:
            break

    db.conn.commit()
    _log.info("discovered_relations: %d new, %d skipped (existing)",
              len(discovered), skipped)
    return {"discovered": discovered, "skipped_existing": skipped}


def _infer_relation_type(type_a: str, type_b: str) -> Optional[str]:
    """从两个概念类型推断关系类型"""
    types = {type_a, type_b}

    # tool + technique → implements
    if "tool" in types and "technique" in types:
        return "implements"

    # vulnerability + technique → exploits
    if "vulnerability" in types and "technique" in types:
        return "exploits"

    # tool + vulnerability → detects/mitigates
    if "tool" in types and "vulnerability" in types:
        return "detects"

    # technique + technique → composes (组合使用)
    if type_a == "technique" and type_b == "technique":
        return "composes"

    # vulnerability + vulnerability → composes (漏洞链)
    if type_a == "vulnerability" and type_b == "vulnerability":
        return "composes"

    # agent_role + technique → produces
    if "agent_role" in types and "technique" in types:
        return "produces"

    # agent_role + tool → uses
    if "agent_role" in types and "tool" in types:
        return "uses"

    # domain + anything → part_of
    if "domain" in types:
        return "part_of"

    # fallback: 同类型 → equivalent_to (低置信度)
    if type_a == type_b:
        return "equivalent_to"

    return None


def _determine_direction(name_a, name_b, id_a, id_b, type_a, type_b, relation_type):
    """决定关系的 from→to 方向"""
    # implements: tool → technique
    if relation_type == "implements":
        if type_a == "tool":
            return name_a, name_b, id_a, id_b
        return name_b, name_a, id_b, id_a

    # exploits: technique → vulnerability
    if relation_type == "exploits":
        if type_a == "technique":
            return name_a, name_b, id_a, id_b
        return name_b, name_a, id_b, id_a

    # detects: tool → vulnerability
    if relation_type == "detects":
        if type_a == "tool":
            return name_a, name_b, id_a, id_b
        return name_b, name_a, id_b, id_a

    # uses: agent_role → tool
    if relation_type == "uses":
        if type_a == "agent_role":
            return name_a, name_b, id_a, id_b
        return name_b, name_a, id_b, id_a

    # produces: agent_role → technique
    if relation_type == "produces":
        if type_a == "agent_role":
            return name_a, name_b, id_a, id_b
        return name_b, name_a, id_b, id_a

    # part_of: anything → domain
    if relation_type == "part_of":
        if type_b == "domain":
            return name_a, name_b, id_a, id_b
        return name_b, name_a, id_b, id_a

    # 默认: 字母序
    if name_a <= name_b:
        return name_a, name_b, id_a, id_b
    return name_b, name_a, id_b, id_a
