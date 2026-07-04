"""
Wisdom 蒸馏 — 从 L4 知识自动提取 distilled_rules

L4 Wisdom 是 DIKW 金字塔的顶层。当知识被提升到 L4（高置信度 + 多源交叉验证），
意味着它已经被充分验证，可以作为可复用的策略规则。

本模块分析 L4 条目，提取模式，写入 distilled_rules 表。

蒸馏策略:
1. 同类型 + 同领域的多条 L4 知识 → 提取共性 → 规则
2. 高 trust_vector 的单条 L4 知识 → 直接转为规则
3. counter_examples 用于过滤: 有 >3 反例的模式不蒸馏

用法:
    from src.governance.wisdom import distill_wisdom
    rules = distill_wisdom(db)
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

_log = logging.getLogger("swarm_knowledge.wisdom")

# 蒸馏阈值
MIN_TRUST_FOR_RULE = 0.80          # 单条 L4 的 trust 分数门槛
MIN_L4_ENTRIES_FOR_PATTERN = 2     # 提取模式规则至少需要的 L4 条目数
MAX_COUNTER_EXAMPLES = 3           # 超过此反例数的模式跳过


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


def distill_wisdom(db) -> Dict[str, Any]:
    """
    从 L4 知识蒸馏 Wisdom 规则。

    返回:
        {"distilled": [...], "skipped": N}
    """
    # 获取所有 L4 条目
    l4_entries = db.fetch_all(
        """SELECT id, title, content, knowledge_type, domain, knowledge_intent,
                  trust_vector, tags, source_agent
           FROM knowledge_entries
           WHERE level = 4 AND status = 'active'
           ORDER BY created_at DESC LIMIT 100"""
    )

    if not l4_entries:
        _log.info("wisdom_distill: no L4 entries, nothing to distill")
        return {"distilled": [], "skipped": 0, "reason": "no_l4_entries"}

    distilled = []
    skipped = 0

    # 策略 1: 按 (domain, knowledge_type, knowledge_intent) 分组提取模式
    groups = _group_l4_entries(l4_entries)

    for group_key, entries in groups.items():
        if len(entries) < MIN_L4_ENTRIES_FOR_PATTERN:
            continue

        # 检查反例
        total_ce = 0
        for e in entries:
            ce = db.fetch_one(
                "SELECT COUNT(*) AS c FROM counter_examples WHERE knowledge_id = ?",
                (e["id"],),
            )
            total_ce += ce["c"] if ce else 0

        if total_ce > MAX_COUNTER_EXAMPLES:
            _log.info("wisdom_distill: skipping group %s (too many counter_examples: %d)",
                      group_key, total_ce)
            skipped += 1
            continue

        # 提取规则
        rule = _extract_pattern_rule(db, group_key, entries)
        if rule:
            distilled.append(rule)

    # 策略 2: 高 trust 的单条 L4 → 直接转规则
    for entry in l4_entries:
        trust = compute_trust_score(entry["trust_vector"] or "{}")
        if trust < MIN_TRUST_FOR_RULE:
            continue

        # 检查是否已被提取过
        existing = db.fetch_one(
            "SELECT id FROM distilled_rules WHERE source_knowledge_ids LIKE ?",
            (f'%{entry["id"]}%',),
        )
        if existing:
            continue

        rule = _extract_single_rule(db, entry, trust)
        if rule:
            distilled.append(rule)

    _log.info("wisdom_distill: %d rules distilled, %d groups skipped",
              len(distilled), skipped)
    return {"distilled": distilled, "skipped": skipped}


def _group_l4_entries(entries: List[Dict]) -> Dict[str, List[Dict]]:
    """按 (domain, knowledge_type, knowledge_intent) 分组"""
    groups = {}
    for e in entries:
        key = (e["domain"] or "general", e["knowledge_type"], e["knowledge_intent"] or "understand")
        groups.setdefault(key, []).append(e)
    return groups


def _extract_pattern_rule(db, group_key: tuple, entries: List[Dict]) -> Optional[Dict]:
    """从一组 L4 条目提取模式规则"""
    domain, ktype, intent = group_key
    entry_ids = [e["id"] for e in entries]

    # 规则名: domain:type:intent
    rule_name = f"wisdom:{domain}:{ktype}:{intent}"

    # 检查是否已存在
    existing = db.fetch_one("SELECT id FROM distilled_rules WHERE rule_name = ?", (rule_name,))
    if existing:
        return None

    # 提取共性 tags
    all_tags = set()
    for e in entries:
        try:
            tags = json.loads(e["tags"]) if isinstance(e["tags"], str) else (e["tags"] or [])
            all_tags.update(tags)
        except (json.JSONDecodeError, TypeError):
            pass

    # 构建规则体: 总结条目内容
    summaries = []
    for e in entries[:5]:
        snippet = (e["title"] or e["content"][:120])
        summaries.append(f"- {snippet}")

    rule_body = (
        f"从 {len(entries)} 条已验证知识提取:\n"
        f"领域: {domain}\n类型: {ktype}\n意图: {intent}\n"
        f"关键标签: {', '.join(sorted(all_tags)[:10])}\n\n"
        + "\n".join(summaries)
    )

    rule_id = str(uuid.uuid4())
    rule_type = _map_rule_type(ktype, intent)
    applicable_agents = _map_applicable_agents(intent)

    db.execute(
        """INSERT INTO distilled_rules
           (id, rule_name, rule_description, rule_type, rule_body,
            source_knowledge_ids, distilled_by, applicable_agents, priority,
            is_active, auto_distilled, source_pattern)
           VALUES (?, ?, ?, ?, ?, ?, 'wisdom-engine', ?, ?, 1, 1, ?)""",
        (
            rule_id, rule_name,
            f"Auto-distilled from {len(entries)} L4 entries ({domain}/{ktype}/{intent})",
            rule_type,
            rule_body,
            json.dumps(entry_ids),
            json.dumps(applicable_agents),
            75,  # Wisdom 规则优先级较高
            json.dumps({"group_key": list(group_key), "tags": sorted(all_tags)[:10]}),
        ),
    )
    db.conn.commit()

    return {
        "rule_id": rule_id[:8],
        "rule_name": rule_name,
        "type": rule_type,
        "from_entries": len(entries),
        "tags": sorted(all_tags)[:5],
    }


def _extract_single_rule(db, entry: Dict, trust: float) -> Optional[Dict]:
    """从单条高 trust L4 知识提取规则"""
    rule_name = f"wisdom:{entry['id'][:8]}:{entry['knowledge_type']}"

    # 跳过如果太短
    content = entry["content"]
    if len(content) < 100:
        return None

    rule_id = str(uuid.uuid4())
    rule_type = _map_rule_type(entry["knowledge_type"], entry["knowledge_intent"])
    applicable_agents = _map_applicable_agents(entry["knowledge_intent"])

    db.execute(
        """INSERT INTO distilled_rules
           (id, rule_name, rule_description, rule_type, rule_body,
            source_knowledge_ids, distilled_by, applicable_agents, priority,
            is_active, auto_distilled, distilled_from_knowledge_ids)
           VALUES (?, ?, ?, ?, ?, ?, 'wisdom-engine', ?, ?, 1, 1, ?)""",
        (
            rule_id, rule_name,
            f"Distilled from single L4 entry (trust={trust:.2f}): {entry['title'][:80]}",
            rule_type,
            content[:500],
            json.dumps([entry["id"]]),
            json.dumps(applicable_agents),
            int(trust * 100),
            json.dumps([entry["id"]]),
        ),
    )
    db.conn.commit()

    return {
        "rule_id": rule_id[:8],
        "rule_name": rule_name,
        "type": rule_type,
        "from_entries": 1,
        "trust": round(trust, 2),
    }


def _map_rule_type(ktype: str, intent: str) -> str:
    """知识类型+意图 → 规则类型"""
    if intent == "attack" or ktype == "vulnerability":
        return "best_practice"
    if intent == "defend":
        return "constraint"
    if ktype == "strategy":
        return "strategy"
    if ktype == "pattern":
        return "heuristic"
    return "best_practice"


def _map_applicable_agents(intent: str) -> List[str]:
    """意图 → 适用角色"""
    mapping = {
        "attack": ["exploiter", "analyst"],
        "defend": ["reporter"],
        "enumerate": ["scanner"],
        "understand": ["analyst"],
        "optimize": ["orchestrator"],
    }
    return mapping.get(intent, ["analyst", "scanner"])
