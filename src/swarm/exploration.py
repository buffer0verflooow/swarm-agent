"""
Exploration Traces — 蜂群探索路径记忆 (Phase A)

Phase A 设计原则:
  1. 粗粒度 — 记录 literal target_url，不做语义归一化
  2. 信息型 — 不自动阻止任务生成，Agent 自行判断是否重复
  3. 可审计 — 每条记录关联 run_id + task_id + agent_id
  4. 跨 run 复用 — 同一 target 的多次 run 共享探索历史

Phase B/C 会在积累足够数据后引入语义归一化和自动决策。

用法:
    from src.swarm.exploration import record_trace, build_exploration_context

    # Agent 完成测试后记录
    record_trace(db, run_id="r-001", task_id="t-001", agent_id="agent-01",
                 target_url="https://api.target.com/users/123",
                 method="GET", vuln_class="IDOR", result="not_found",
                 depth="medium", notes="Tested role escalation with modified tokens")

    # Agent 启动时注入上下文
    ctx = build_exploration_context(db, run_id="r-001")
    prompt = f"{base_prompt}\\n\\n{ctx}"
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

_log = logging.getLogger("swarm_knowledge.exploration")

# ── 常量 ──

EXHAUSTION_THRESHOLD = 3      # 同一路径被测试 N 次且全部 not_found → 视为已穷尽
CONTEXT_MAX_ITEMS = 15        # 注入 Agent 上下文的最大条目数
CONTEXT_MAX_PER_CLASS = 5     # 每种 vulnerability_class 最多注入 N 条


# ── Public API ──

def record_trace(
    db,
    *,
    run_id: str = "",
    task_id: str = "",
    agent_id: str = "",
    target_url: str,
    method: str = "GET",
    vulnerability_class: str = "unknown",
    result: str = "inconclusive",
    finding_id: str = "",
    depth: str = "shallow",
    notes: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> str:
    """记录一条探索轨迹。

    Args:
        target_url: 被测试的 literal URL（不做归一化）
        vulnerability_class: IDOR / SQLi / XSS / auth_bypass / open_redirect / ...
        result: found / not_found / blocked / error / inconclusive
        depth: shallow / medium / deep
        finding_id: 如果 result='found'，关联的 knowledge_entry id
    """
    trace_id = str(uuid.uuid4())
    meta_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True, default=str)

    db.execute(
        """INSERT INTO exploration_traces
           (trace_id, run_id, task_id, agent_id, target_url, method,
            vulnerability_class, result, finding_id, depth, notes, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trace_id,
            run_id or "",
            task_id or "",
            agent_id or "",
            target_url,
            method.upper(),
            vulnerability_class.lower(),
            result,
            finding_id or "",
            depth,
            notes,
            meta_json,
        ),
    )
    if commit:
        db.conn.commit()

    _log.debug("exploration trace: %s | %s %s | %s | %s",
               trace_id[:8], method, target_url[:60], vulnerability_class, result)
    return trace_id


def get_explored_for_target(
    db,
    target_url: str,
    vulnerability_class: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """查询某个 target_url 的探索历史。

    Args:
        target_url: 精确匹配（literal URL）
        vulnerability_class: 可选过滤
        run_id: 可选过滤
    """
    conditions = ["target_url = ?"]
    params: List[Any] = [target_url]

    if vulnerability_class:
        conditions.append("vulnerability_class = ?")
        params.append(vulnerability_class)
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)

    sql = f"""SELECT trace_id, run_id, task_id, agent_id, target_url, method,
                     vulnerability_class, result, depth, notes, created_at
              FROM exploration_traces
              WHERE {" AND ".join(conditions)}
              ORDER BY created_at DESC
              LIMIT ?"""
    params.append(limit)

    return [dict(r) for r in db.fetch_all(sql, tuple(params))]


def get_exploration_summary(db, run_id: Optional[str] = None) -> Dict[str, Any]:
    """获取探索摘要统计。

    返回:
        {
            total_traces: int,
            by_result: {found: N, not_found: N, blocked: N, error: N, inconclusive: N},
            by_vuln_class: {IDOR: N, SQLi: N, ...},
            unique_targets: int,
            unique_coverage: int,  -- 不重复的 (target × vuln_class) 组合数
        }
    """
    conditions = ""
    params: List[Any] = []
    if run_id:
        conditions = "WHERE run_id = ?"
        params.append(run_id)

    total = db.fetch_one(
        f"SELECT COUNT(*) AS c FROM exploration_traces {conditions}", tuple(params)
    )
    by_result_rows = db.fetch_all(
        f"SELECT result, COUNT(*) AS c FROM exploration_traces {conditions} GROUP BY result",
        tuple(params),
    )
    by_vuln_rows = db.fetch_all(
        f"SELECT vulnerability_class, COUNT(*) AS c FROM exploration_traces {conditions} GROUP BY vulnerability_class ORDER BY c DESC",
        tuple(params),
    )
    unique_targets = db.fetch_one(
        f"SELECT COUNT(DISTINCT target_url) AS c FROM exploration_traces {conditions}",
        tuple(params),
    )
    unique_coverage = db.fetch_one(
        f"SELECT COUNT(DISTINCT target_url || '|' || vulnerability_class) AS c FROM exploration_traces {conditions}",
        tuple(params),
    )

    return {
        "total_traces": total["c"] if total else 0,
        "by_result": {r["result"]: r["c"] for r in by_result_rows},
        "by_vuln_class": {r["vulnerability_class"]: r["c"] for r in by_vuln_rows},
        "unique_targets": unique_targets["c"] if unique_targets else 0,
        "unique_coverage": unique_coverage["c"] if unique_coverage else 0,
    }


def get_exhausted_paths(
    db,
    run_id: Optional[str] = None,
    threshold: int = EXHAUSTION_THRESHOLD,
) -> List[Dict[str, Any]]:
    """找出已经被多次测试且全部无发现的路径。

    同一 (target_url × vulnerability_class) 被测试 >= threshold 次，
    且全部 result='not_found' → 标记为 exhausted。

    Args:
        threshold: 至少被测试 N 次才判定为 exhausted
    """
    conditions = ""
    params: List[Any] = []
    if run_id:
        conditions = "AND run_id = ?"
        params.append(run_id)

    params.extend([threshold])
    sql = f"""SELECT target_url, vulnerability_class,
                     COUNT(*) AS total_attempts,
                     SUM(CASE WHEN result = 'found' THEN 1 ELSE 0 END) AS found_count,
                     MAX(depth) AS max_depth,
                     MAX(created_at) AS last_attempt_at
              FROM exploration_traces
              WHERE 1=1 {conditions}
              GROUP BY target_url, vulnerability_class
              HAVING COUNT(*) >= ?
                 AND SUM(CASE WHEN result = 'found' THEN 1 ELSE 0 END) = 0
                 AND MAX(CASE WHEN result = 'not_found' THEN 1 ELSE 0 END) = 1
              ORDER BY total_attempts DESC"""

    rows = db.fetch_all(sql, tuple(params))
    return [dict(r) for r in rows]


def build_exploration_context(
    db,
    run_id: str,
    max_items: int = CONTEXT_MAX_ITEMS,
    max_per_class: int = CONTEXT_MAX_PER_CLASS,
) -> str:
    """构建 Agent 上下文注入字符串。

    供 Orchestrator._build_spawn_context() 调用。
    只注入「已测试且未发现漏洞」的路径摘要，避免 Agent 重复工作。

    格式:
        ## 蜂群探索记忆（已测试路径）

        ### 已有发现 (result=found)
        - [IDOR] GET /api/users/:id → ✅ 发现 (deep, task=t-001)

        ### 已测试无发现
        - [SQLi] POST /api/login → ❌ 无发现 (shallow, 2 agents tried)

        ### 已穷尽路径 (>=3 次测试无发现)
        - [open_redirect] GET /redirect → ⛔ 已穷尽 (3 attempts, 全部无发现)
    """
    if not run_id:
        return ""

    # 获取本 run 的所有痕迹
    traces = db.fetch_all(
        """SELECT target_url, method, vulnerability_class, result, depth, agent_id, notes,
                  (SELECT COUNT(*) FROM exploration_traces e2
                   WHERE e2.target_url = e1.target_url
                     AND e2.vulnerability_class = e1.vulnerability_class) AS attempt_count
           FROM exploration_traces e1
           WHERE run_id = ?
           ORDER BY
             CASE result
               WHEN 'found' THEN 0
               WHEN 'blocked' THEN 1
               WHEN 'not_found' THEN 2
               ELSE 3
             END,
             created_at DESC
           LIMIT ?""",
        (run_id, max_items * 3),  # 取多一点用于后续分组过滤
    )

    if not traces:
        return ""

    # 按 vulnerability_class 分组
    by_class: Dict[str, List[Dict]] = {}
    for t in [dict(r) for r in traces]:
        vc = t["vulnerability_class"]
        by_class.setdefault(vc, []).append(t)

    parts = ["## 蜂群探索记忆（已测试路径）\n"]

    # ① 已有发现
    found_items = [t for t in [dict(r) for r in traces] if t["result"] == "found"]
    if found_items:
        parts.append("### 已有发现 ✅")
        for t in found_items[:max_items]:
            parts.append(
                f"- [{t['vulnerability_class'].upper()}] {t['method']} {t['target_url'][:80]} "
                f"→ 已发现漏洞 (depth={t['depth']})"
            )
        parts.append("")

    # ② 已测试无发现（按 class 分组，每组最多 max_per_class）
    not_found_by_class: Dict[str, List[Dict]] = {}
    for vc, items in by_class.items():
        nf = [t for t in items if t["result"] == "not_found"]
        if nf:
            not_found_by_class[vc] = nf

    if not_found_by_class:
        parts.append("### 已测试无发现 ❌")
        shown = 0
        for vc, items in sorted(not_found_by_class.items()):
            if shown >= max_items:
                break
            for t in items[:max_per_class]:
                if shown >= max_items:
                    break
                # 计算同一 target × vuln 的总尝试次数
                attempt_count = t.get("attempt_count", 1)
                count_str = f"({attempt_count} agent{'s' if attempt_count > 1 else ''} tried)" if attempt_count > 1 else ""
                parts.append(
                    f"- [{vc.upper()}] {t['method']} {t['target_url'][:80]} "
                    f"→ 无发现 (depth={t['depth']}) {count_str}"
                )
                shown += 1
        parts.append("")

    # ③ 已穷尽路径（>= threshold 次无发现）
    exhausted = get_exhausted_paths(db, run_id=run_id)
    if exhausted:
        parts.append("### 已穷尽路径 ⛔ (>=3次无发现，建议不再测试)")
        for e in exhausted[:5]:
            parts.append(
                f"- [{e['vulnerability_class'].upper()}] {e['target_url'][:80]} "
                f"→ 已穷尽 ({e['total_attempts']} attempts, max_depth={e['max_depth']})"
            )
        parts.append("")

    # ④ 被阻塞的路径
    blocked_items = [t for t in [dict(r) for r in traces] if t["result"] == "blocked"]
    if blocked_items:
        parts.append("### 被 WAF/CF 阻塞的路径 🚫")
        for t in blocked_items[:5]:
            parts.append(
                f"- [{t['vulnerability_class'].upper()}] {t['method']} {t['target_url'][:80]} "
                f"→ 被阻塞"
            )
        parts.append("")

    if len(parts) == 1:
        return ""  # 只有标题，没有内容

    return "\n".join(parts)


def get_unexplored_hints(
    db,
    run_id: str,
    known_endpoints: List[str],
    known_vuln_classes: Optional[List[str]] = None,
) -> str:
    """给定已知端点列表，返回尚未测试的 (endpoint × vuln_class) 组合建议。

    这是"提示型"函数——告诉 Agent 还有哪些路径没探索，但不强制。

    Args:
        known_endpoints: 当前已知的端点列表 (literal URLs)
        known_vuln_classes: 要检查的漏洞类型列表，默认常用 10 种
    """
    if not known_endpoints:
        return ""

    default_classes = [
        "IDOR", "SQLi", "XSS", "auth_bypass", "open_redirect",
        "csrf", "ssrf", "ssti", "lfi", "information_disclosure",
    ]
    classes = known_vuln_classes or default_classes

    explored = db.fetch_all(
        """SELECT DISTINCT target_url, vulnerability_class
           FROM exploration_traces
           WHERE run_id = ? AND target_url IN ({})
           GROUP BY target_url, vulnerability_class""".format(
            ",".join("?" for _ in known_endpoints)
        ),
        (run_id, *known_endpoints),
    )
    explored_set = {(r["target_url"], r["vulnerability_class"]) for r in explored}

    unexplored = []
    for ep in known_endpoints:
        for vc in classes:
            if (ep, vc) not in explored_set:
                unexplored.append((ep, vc))

    if not unexplored:
        return ""

    lines = ["\n## 未探索路径建议 📋"]
    for ep, vc in unexplored[:20]:
        lines.append(f"- [{vc}] {ep[:80]}")

    return "\n".join(lines)


# ── 内部辅助 ──

def _ensure_schema(db) -> None:
    """确保 exploration_traces 表存在（幂等）"""
    try:
        db.execute("SELECT 1 FROM exploration_traces LIMIT 1")
    except Exception:
        _log.info("exploration_traces table not found, applying migration...")
        import os
        mig_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "migrations", "010_exploration_traces.sql",
        )
        if os.path.exists(mig_path):
            sql = open(mig_path).read()
            for stmt in sql.split(";\n"):
                stmt = stmt.strip()
                if stmt and not stmt.startswith("--"):
                    try:
                        db.conn.execute(stmt)
                    except Exception as e:
                        if "already exists" not in str(e):
                            raise
            db.conn.commit()
            _log.info("exploration_traces table created")
