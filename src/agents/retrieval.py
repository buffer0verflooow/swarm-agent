"""
Swarm Knowledge Retrieval — 知识消费层

知识库不只积累，更要「在正确的时间把正确的知识喂给正确的 Agent」。

三种消费模式:
1. 主动检索 (pull):  Agent 在任务前/中主动查询
2. 上下文注入 (push): 编排器在启动 Agent 时自动注入相关背景
3. 策略路由 (route): 基于历史成功率自动选择最佳策略
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


def _sanitize_fts_query(query: str) -> str:
    """Sanitize a raw query string for safe FTS5 MATCH.
    
    FTS5 special chars: " * + - AND OR NOT NEAR ( ) :
    These cause syntax errors or unexpected behavior if passed raw.
    Strategy: extract alphanumeric words >=2 chars, filter FTS5 reserved words,
    join with implicit AND (space-separated = AND in FTS5 by default).
    """
    # FTS5 reserved operators that must not appear as bare words
    fts5_reserved = {"AND", "OR", "NOT", "NEAR", "END", "BEGIN"}
    
    words = re.findall(r'[a-zA-Z0-9_\u4e00-\u9fff]{2,}', query[:200])
    # Filter out reserved words (case-sensitive match for uppercase operators)
    safe_words = [w for w in words if w.upper() not in fts5_reserved]
    
    if not safe_words:
        # Fallback: try shorter matches
        words = re.findall(r'[^\s"\'()*+\-:;]+', query[:200])
        safe_words = [w for w in words if w.upper() not in fts5_reserved]
    
    if not safe_words:
        return ""
    
    # Use space-separated (implicit AND) instead of OR to be more precise
    # But OR is safer for recall when we only have 2-3 words
    return " OR ".join(safe_words[:5])


# ============================================================================
# 1. 主动检索 — Agent 说 "我需要知道..."
# ============================================================================

def search(
    db,
    query: str,
    domain: Optional[str] = None,
    level_min: int = 2,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """
    全文 + 语义检索。

    Agent 使用示例:
        "search('如何绕过 ASLR', domain='security', level_min=3)"
        → 返回已验证的高置信度知识
    """
    # Sanitize FTS5 query to prevent syntax errors / injection
    safe_query = _sanitize_fts_query(query)
    if not safe_query:
        return []

    rows = db.fetch_all(
        """SELECT ke.id, ke.title, ke.level, ke.knowledge_type, ke.domain,
                  ke.content, ke.tags, ke.trust_vector, ke.created_at,
                  snippet(knowledge_entries_fts, 1, '<b>', '</b>', '...', 40) AS snippet
           FROM knowledge_entries_fts fts
           JOIN knowledge_entries ke ON fts.rowid = ke.rowid
           WHERE knowledge_entries_fts MATCH ?
             AND ke.status = 'active'
             AND ke.level >= ?
             """ + ("AND ke.domain = ?" if domain else "") + """
           ORDER BY ke.level DESC, rank
           LIMIT ?""",
        (safe_query, level_min, *([domain] if domain else []), limit),
    )
    return [_row_to_result(r) for r in rows]


def search_by_tags(
    db,
    tags: List[str],
    domain: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """按标签检索 — 适合 "给我所有关于 port_scan 的经验" """
    conditions = ["status = 'active'"]
    params: List[Any] = []

    for tag in tags:
        conditions.append("tags LIKE ?")
        params.append(f'%"{tag}"%')

    if domain:
        conditions.append("domain = ?")
        params.append(domain)

    where = " AND ".join(conditions)
    params.append(limit)

    rows = db.fetch_all(
        f"SELECT * FROM knowledge_entries WHERE {where} ORDER BY level DESC LIMIT ?",
        tuple(params),
    )
    return [_row_to_result(r) for r in rows]


def get_active_rules(
    db,
    agent_role: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    获取适用于当前场景的活跃规则（Wisdom 层）。

    Agent 使用示例:
        "get_active_rules(agent_role='scanner', intent='recon')"
        → 返回 "先扫存活再扫端口" 这样的策略规则
    """
    conditions = ["is_active = 1"]
    params: List[Any] = []

    if agent_role:
        conditions.append("applicable_agents LIKE ?")
        params.append(f'%"{agent_role}"%')
    if intent:
        conditions.append("trigger_condition LIKE ?")
        params.append(f'%"{intent}"%')

    where = " AND ".join(conditions)
    params.append(limit)

    rows = db.fetch_all(
        f"SELECT * FROM distilled_rules WHERE {where} ORDER BY priority DESC LIMIT ?",
        tuple(params),
    )
    return [dict(r) for r in rows]


def get_similar(
    db,
    entry_id: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """找到与指定条目相似的知识（同标签 + 同领域）"""
    entry = db.fetch_one(
        "SELECT tags, domain FROM knowledge_entries WHERE id = ?",
        (entry_id,),
    )
    if not entry:
        return []

    try:
        tags = json.loads(entry["tags"]) if isinstance(entry["tags"], str) else entry["tags"]
    except (json.JSONDecodeError, TypeError):
        tags = []

    domain = entry["domain"]
    results = []

    # 同领域 + 同标签
    for tag in tags[:3]:
        rows = db.fetch_all(
            """SELECT * FROM knowledge_entries
               WHERE id != ? AND domain = ? AND tags LIKE ? AND status = 'active'
               ORDER BY level DESC LIMIT ?""",
            (entry_id, domain, f'%"{tag}"%', limit),
        )
        results.extend([_row_to_result(r) for r in rows])
        if len(results) >= limit:
            break

    return results[:limit]


# ============================================================================
# 2. 上下文注入 — 编排器说 "启动 scanner 之前，先注入这些知识"
# ============================================================================

def build_context_injection(
    db,
    intent: str,
    target_type: str,
    agent_role: str,
    max_tokens_estimate: int = 2000,
) -> str:
    """
    为即将启动的 Agent 构建知识上下文。

    编排器使用:
        context = build_context_injection(db, intent='recon', target_type='ip', agent_role='scanner')
        agent_prompt = f"{context}\n\n{original_prompt}"

    注入策略:
    1. 先查策略规则 (Wisdom) — 告诉 Agent "怎么干"
    2. 再查同类经验 (Knowledge) — 告诉 Agent "别人怎么干的"
    3. 最后查相关事实 (Info) — 告诉 Agent "已知什么"
    """
    parts = []
    tokens_used = 0

    # Layer 1: Wisdom — 最优策略
    rules = get_active_rules(db, agent_role=agent_role, intent=intent, limit=3)
    if rules:
        parts.append("## 已知最优策略")
        for r in rules:
            snippet = r.get("rule_body", "")[:300]
            parts.append(f"- **{r['rule_name']}** (priority={r['priority']}): {snippet}")
            tokens_used += len(snippet) // 3
            if tokens_used > max_tokens_estimate // 3:
                break

    # Layer 2: Knowledge — 同类经验
    similar_entries = search_by_tags(
        db,
        tags=_intent_to_tags(intent, target_type),
        limit=5,
    )
    if similar_entries:
        parts.append("\n## 相关经验")
        for e in similar_entries:
            snippet = (e.get("title", "") or e.get("content", ""))[:200]
            parts.append(f"- [L{e['level']}] {snippet}")
            tokens_used += len(snippet) // 3
            if tokens_used > max_tokens_estimate * 2 // 3:
                break

    # Layer 3: Information — 已知背景
    domain_entries = db.fetch_all(
        """SELECT title, content FROM knowledge_entries
           WHERE domain = (SELECT domain FROM knowledge_entries ORDER BY created_at DESC LIMIT 1)
             AND level = 2 AND status = 'active'
           ORDER BY created_at DESC LIMIT 2""",
    )
    if domain_entries:
        parts.append("\n## 背景信息")
        for e in domain_entries:
            snippet = e["content"][:150]
            parts.append(f"- {snippet}")

    return "\n".join(parts) if parts else ""


def _intent_to_tags(intent: str, target_type: str) -> List[str]:
    """意图 → 标签映射"""
    mapping = {
        "recon": ["port_scan", "nmap", "osint", "enumeration"],
        "exploit": ["exploit", "cve", "vulnerability", "payload"],
        "analyze": ["reverse_engineering", "static_analysis", "dynamic_analysis"],
        "defend": ["mitigation", "patch", "firewall"],
        "report": ["writeup", "documentation"],
    }
    base = mapping.get(intent, [intent])
    if target_type:
        base.append(target_type)
    return base


# ============================================================================
# 3. 策略路由 — 编排器说 "这么多方案，用哪个？"
# ============================================================================

def select_best_strategy(
    db,
    intent: str,
    target_type: str,
    complexity: str = "medium",
) -> Optional[Dict[str, Any]]:
    """
    基于历史成功率选择最佳策略。
    """
    rows = db.fetch_all(
        """SELECT s.*,
                  COUNT(sa.application_id) AS total_applications,
                  SUM(CASE WHEN sa.outcome = 'success' THEN 1 ELSE 0 END) AS successes
           FROM swarm_strategies s
           LEFT JOIN strategy_applications sa ON s.strategy_id = sa.strategy_id
           WHERE s.is_active = 1
             AND s.trigger_intent = ?
             AND s.trigger_target_type = ?
             AND s.trigger_complexity = ?
           GROUP BY s.strategy_id
           ORDER BY
             CASE WHEN s.use_count > 0
                  THEN CAST(s.success_count AS REAL) / s.use_count
                  ELSE CAST(s.priority AS REAL) / 100
             END DESC
           LIMIT 1""",
        (intent, target_type, complexity),
    )

    if rows:
        return dict(rows[0])
    return None


def record_strategy_outcome(
    db,
    strategy_id: str,
    run_id: str,
    agent_name: str,
    outcome: str,  # success / partial / failure
    duration_ms: int = 0,
):
    """记录策略执行结果 — 反馈循环的关键"""
    import uuid
    db.execute(
        """INSERT INTO strategy_applications (application_id, strategy_id, run_id, applied_by, outcome, duration_ms)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), strategy_id, run_id, agent_name, outcome, duration_ms),
    )
    db.execute(
        "UPDATE swarm_strategies SET use_count = use_count + 1, "
        "success_count = success_count + CASE WHEN ? = 'success' THEN 1 ELSE 0 END "
        "WHERE strategy_id = ?",
        (outcome, strategy_id),
    )
    db.conn.commit()


# ============================================================================
# 4. 知识仪表盘 — 回答 "知识库里有什么？"
# ============================================================================

def knowledge_summary(db) -> Dict[str, Any]:
    """知识库概览"""
    return {
        **db.stats(),
        "recently_added": [
            _row_to_result(r)
            for r in db.fetch_all(
                "SELECT * FROM knowledge_entries WHERE status = 'active' ORDER BY created_at DESC LIMIT 5"
            )
        ],
        "top_domains": db.stats().get("by_domain", {}),
        "ready_rules": db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM distilled_rules WHERE is_active = 1 AND counter_example_count < 5"
        )["cnt"],
    }


def query(
    db,
    intent: str,
    target_type: str,
    agent_role: str,
) -> Dict[str, Any]:
    """
    一站式查询 — Agent 在任务开始前调用。

    返回:
        {
            "best_strategy": {...},
            "context_injection": "...",
            "similar_experiences": [...],
            "relevant_rules": [...]
        }
    """
    return {
        "best_strategy": select_best_strategy(db, intent, target_type),
        "context_injection": build_context_injection(db, intent, target_type, agent_role),
        "similar_experiences": search_by_tags(
            db, _intent_to_tags(intent, target_type), limit=5
        ),
        "relevant_rules": get_active_rules(db, agent_role=agent_role, intent=intent),
    }


# ============================================================================
# Helpers
# ============================================================================

def _row_to_result(row) -> Dict[str, Any]:
    """标准化输出"""
    d = dict(row)
    # Parse JSON fields
    for field in ("tags", "trust_vector"):
        if field in d and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
