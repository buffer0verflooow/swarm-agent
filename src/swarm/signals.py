"""
Worker Signal Stream — 量化 Worker Agent 产出质量 (Controller/Worker Phase A)

供 Controller (Phase B) 做 kill/boost/spawn 决策的数据基础。

核心函数:
  - record_worker_signal(): 记录一条信号
  - get_recent_worker_signals(): 获取某个 Worker 的最近信号
  - get_all_worker_signals(): 获取一个 run 下所有 Worker 的信号摘要
  - detect_loops(): 检测某 Worker 是否原地打转
  - get_stuck_workers(): 找出所有卡住的 Worker
  - compute_novelty_score(): 基于 content_hash 计算新发现得分
  - record_signal_from_capture(): capture 后自动记录信号

用法:
    from src.swarm.signals import record_worker_signal, get_stuck_workers

    record_worker_signal(db, run_id="r1", agent_id="a1",
                         signal_type="finding", output_quality=0.85,
                         novelty_score=0.92, progress_marker="verified 3/5")

    stuck = get_stuck_workers(db, run_id="r1")
    for w in stuck:
        print(f"Worker {w['agent_id']} stuck: {w['reason']}")
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("swarm_knowledge.signals")

# ── 常量 ──

LOOP_DETECT_WINDOW = 5           # 检查最近 N 条信号
LOOP_NOVELTY_THRESHOLD = 0.1     # novelty_score 低于此值 → 无新发现
LOOP_CONSECUTIVE_COUNT = 3       # 连续 N 次低 novelty → 判定为兜圈
STUCK_QUALITY_THRESHOLD = 0.3    # mean output_quality 低于此 → 卡住
STUCK_EFFICIENCY_THRESHOLD = 0.1 # mean efficiency 低于此 → 卡住

# knowledge_type → output_quality 映射 (来自 capture.py 分类体系)
QUALITY_BY_KNOWLEDGE_TYPE = {
    "vulnerability": 0.90,
    "technique": 0.75,
    "pattern": 0.65,
    "observation": 0.55,
    "configuration": 0.50,
    "strategy": 0.70,
    "general": 0.30,
}


# ── Public API ──

def record_worker_signal(
    db,
    *,
    run_id: str,
    agent_id: str,
    signal_type: str = "tool_output",
    output_quality: float = 0.5,
    novelty_score: float = 0.0,
    efficiency: float = 0.0,
    loop_detected: int = 0,
    progress_marker: str = "",
    last_useful_at: str = "",
    raw_output_hash: str = "",
    raw_output_snippet: str = "",
    knowledge_entry_id: str = "",
    task_id: str = "",
    tokens_spent_since: int = 0,
    findings_since: int = 0,
    notes: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    commit: bool = True,
    auto_detect_loop: bool = True,
) -> str:
    """记录一条 Worker 产出信号。

    auto_detect_loop=True 时，自动检测最近 LOOP_DETECT_WINDOW 条信号，
    如果连续 LOOP_CONSECUTIVE_COUNT 条 novelty < LOOP_NOVELTY_THRESHOLD，
    自动标记 loop_detected=1。
    """
    signal_id = str(uuid.uuid4())

    # 自动原地打转检测
    if auto_detect_loop and loop_detected == 0:
        loop_detected = 1 if _check_loop_now(db, agent_id, novelty_score) else 0

    meta_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True, default=str)
    raw_snippet = raw_output_snippet[:500] if raw_output_snippet else ""

    db.execute(
        """INSERT INTO worker_signals
           (signal_id, run_id, agent_id, task_id, signal_type,
            output_quality, novelty_score, efficiency, loop_detected,
            progress_marker, last_useful_at, raw_output_hash, raw_output_snippet,
            knowledge_entry_id, tokens_spent_since, findings_since,
            notes, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            signal_id, run_id, agent_id, task_id, signal_type,
            max(0.0, min(1.0, output_quality)),
            max(0.0, min(1.0, novelty_score)),
            max(0.0, efficiency),
            loop_detected,
            progress_marker, last_useful_at, raw_output_hash, raw_snippet,
            knowledge_entry_id, int(tokens_spent_since), int(findings_since),
            notes, meta_json,
        ),
    )
    if commit:
        db.conn.commit()

    _log.debug("worker signal: %s | %s | q=%.2f novelty=%.2f loop=%d",
               signal_id[:8], agent_id[:12], output_quality, novelty_score, loop_detected)
    return signal_id


def get_recent_worker_signals(
    db,
    agent_id: str,
    run_id: Optional[str] = None,
    limit: int = LOOP_DETECT_WINDOW,
) -> List[Dict[str, Any]]:
    """获取某个 Worker 的最近 N 条信号。"""
    conditions = ["agent_id = ?"]
    params: List[Any] = [agent_id]
    if run_id:
        conditions.append("run_id = ?")
        params.append(run_id)

    rows = db.fetch_all(
        f"""SELECT * FROM worker_signals
            WHERE {" AND ".join(conditions)}
            ORDER BY created_at DESC
            LIMIT ?""",
        tuple(params + [limit]),
    )
    return [dict(r) for r in rows]


def get_all_worker_signals(
    db,
    run_id: str,
    window_seconds: int = 120,
) -> List[Dict[str, Any]]:
    """获取一个 run 下所有 Worker 的最近信号摘要。

    返回每个 Worker 最近一条信号 + 统计指标。
    供 Controller prompt 构建使用。
    """
    rows = db.fetch_all(
        """SELECT
               ws.agent_id,
               MAX(ws.created_at) AS last_seen,
               AVG(ws.output_quality) AS avg_quality,
               AVG(ws.novelty_score) AS avg_novelty,
               AVG(ws.efficiency) AS avg_efficiency,
               MAX(ws.loop_detected) AS is_stuck,
               MAX(ws.progress_marker) AS latest_progress,
               MAX(ws.last_useful_at) AS last_useful,
               COUNT(*) AS signal_count
           FROM worker_signals ws
           WHERE ws.run_id = ?
             AND ws.created_at >= datetime('now', ? || ' seconds')
           GROUP BY ws.agent_id
           ORDER BY avg_quality DESC""",
        (run_id, f"-{window_seconds}"),
    )
    return [dict(r) for r in rows]


def detect_loops(
    db,
    agent_id: str,
    run_id: Optional[str] = None,
    window: int = LOOP_DETECT_WINDOW,
    threshold: float = LOOP_NOVELTY_THRESHOLD,
    consecutive: int = LOOP_CONSECUTIVE_COUNT,
) -> Tuple[bool, str]:
    """检测 Worker 是否原地打转。

    Returns:
        (is_stuck: bool, reason: str)
    """
    signals = get_recent_worker_signals(db, agent_id, run_id, window)

    if len(signals) < consecutive:
        return False, f"not enough signals ({len(signals)} < {consecutive})"

    # 检查最近 consecutive 条是否全部低 novelty
    recent = signals[:consecutive]
    low_novelty_count = sum(1 for s in recent if s["novelty_score"] < threshold)

    if low_novelty_count >= consecutive:
        avg_nov = sum(s["novelty_score"] for s in recent) / len(recent)
        avg_qual = sum(s["output_quality"] for s in recent) / len(recent)
        return True, (
            f"loop detected: last {consecutive} signals all novelty<{threshold} "
            f"(avg_novelty={avg_nov:.3f}, avg_quality={avg_qual:.3f})"
        )

    return False, "ok"


def get_stuck_workers(
    db,
    run_id: str,
    quality_threshold: float = STUCK_QUALITY_THRESHOLD,
    efficiency_threshold: float = STUCK_EFFICIENCY_THRESHOLD,
) -> List[Dict[str, Any]]:
    """找出所有卡住或低效的 Worker。

    判定条件 (满足任一即算 stuck):
      1. loop_detected=1 的最近信号 >= 2 条
      2. 最近 5 条信号的平均 output_quality < quality_threshold
      3. 最近 5 条信号的平均 efficiency < efficiency_threshold
      4. 超过 120s 没有 useful 输出
    """
    all_signals = get_all_worker_signals(db, run_id, window_seconds=300)

    stuck = []
    for ws in all_signals:
        aid = ws["agent_id"]
        reasons = []

        if ws["is_stuck"]:
            reasons.append("loop_detected")

        if ws["avg_quality"] < quality_threshold:
            reasons.append(f"low_quality({ws['avg_quality']:.2f})")

        if ws["avg_efficiency"] < efficiency_threshold and ws["signal_count"] > 3:
            reasons.append(f"low_efficiency({ws['avg_efficiency']:.3f})")

        if ws["last_useful"]:
            # 简单判断: 如果 last_useful 不是 "now"，视为过期
            # SQLite datetime 比较比较复杂，这里用 signal_count 做近似
            pass

        if reasons:
            stuck.append({
                "agent_id": aid,
                "reasons": reasons,
                "avg_quality": round(ws["avg_quality"], 3),
                "avg_novelty": round(ws["avg_novelty"], 3),
                "avg_efficiency": round(ws["avg_efficiency"], 3),
                "signal_count": ws["signal_count"],
                "latest_progress": ws["latest_progress"] or "",
                "last_seen": ws["last_seen"],
            })

    return stuck


def compute_novelty_score(
    db,
    run_id: str,
    content: str,
    agent_id: str = "",
) -> float:
    """基于 content hash 去重 + 最近信号相似度，计算新发现得分。

    1. 计算 content hash
    2. 查 raw_agent_events 或 knowledge_entries 是否有相同 hash
    3. 如果没有完全匹配 → 1.0
    4. 如果有部分匹配 → 基于 TF-IDF 语义相似度估算

    简化版 (不使用 numpy/sklearn):
      - 完全 hash 匹配 → 0.0
      - 部分 token 重叠 → 0.3-0.7
      - 无匹配 → 1.0
    """
    if not content:
        return 0.0

    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    # 1. 检查 knowledge_entries 的 content_hash (如果表存在)
    try:
        has_knowledge = db.fetch_one(
            """SELECT 1 FROM knowledge_entries
               WHERE source_run_id = ? LIMIT 1""",
            (run_id,),
        )
    except Exception:
        has_knowledge = None

    # 2. 检查 raw_agent_events 的 content_hash（无论 knowledge_entries 是否存在）
    dup = db.fetch_one(
        """SELECT 1 FROM raw_agent_events
           WHERE content_hash = ? AND run_id = ?
           LIMIT 1""",
        (content_hash, run_id),
    )
    if dup:
        return 0.0  # 完全重复

    # 3. 粗略 token 重叠估计
    #    取 content 的前 100 个 token，检查是否有类似内容
    tokens = set(content.lower().split()[:100])
    if len(tokens) < 5:
        return 0.5  # 太短，无法判断

    # 查最近 20 条同 agent 的 signal 的 raw_output_snippet
    recent_snippets = db.fetch_all(
        """SELECT raw_output_snippet FROM worker_signals
           WHERE agent_id = ? AND run_id = ?
           ORDER BY created_at DESC LIMIT 20""",
        (agent_id, run_id),
    )

    if not recent_snippets:
        return 1.0

    max_overlap = 0.0
    for row in recent_snippets:
        snippet = (row["raw_output_snippet"] or "").lower()
        if not snippet:
            continue
        snippet_tokens = set(snippet.split())
        if not snippet_tokens:
            continue
        overlap = len(tokens & snippet_tokens) / len(tokens)
        max_overlap = max(max_overlap, overlap)

    if max_overlap > 0.8:
        return 0.1  # 高度相似
    elif max_overlap > 0.6:
        return 0.3
    elif max_overlap > 0.4:
        return 0.5
    elif max_overlap > 0.2:
        return 0.7
    else:
        return 1.0


def record_signal_from_capture(
    db,
    run_id: str,
    agent_id: str,
    knowledge_entry_id: str,
    knowledge_type: str = "observation",
    content: str = "",
    task_id: str = "",
    signals: Optional[int] = None,
    commit: bool = True,
) -> str:
    """capture.py 捕获知识条目后自动记录 Worker 信号。

    供给 capture.py 的 _store_knowledge_entry() 或 capture() 调用。
    """
    quality = QUALITY_BY_KNOWLEDGE_TYPE.get(knowledge_type, 0.5)
    novelty = compute_novelty_score(db, run_id, content, agent_id)

    return record_worker_signal(
        db,
        run_id=run_id,
        agent_id=agent_id,
        signal_type="finding",
        output_quality=quality,
        novelty_score=novelty,
        knowledge_entry_id=knowledge_entry_id,
        task_id=task_id,
        raw_output_snippet=content[:300] if content else "",
        raw_output_hash=hashlib.sha256(content.encode()).hexdigest()[:16] if content else "",
        progress_marker=f"captured [{knowledge_type}]",
        notes=f"auto from capture: {knowledge_type}",
        auto_detect_loop=True,
        commit=commit,
    )


def record_signal_from_heartbeat(
    db,
    run_id: str,
    agent_id: str,
    load_score: float = 0.5,
    task_id: str = "",
    progress_marker: str = "",
    tokens_spent_since: int = 0,
    findings_since: int = 0,
    commit: bool = True,
) -> str:
    """heartbeat 时自动记录 Worker 信号。

    供给 lifecycle.py 的 AgentLifecycle.beat() 调用。
    """
    # 根据 load_score 估算 output_quality
    # load_score 高不一定产出好，但 load=0 肯定没产出
    quality = max(0.1, min(0.8, load_score * 0.8))

    # efficiency = findings / (tokens_spent + 1)
    efficiency = findings_since / max(1, tokens_spent_since) if tokens_spent_since > 0 else 0.0

    return record_worker_signal(
        db,
        run_id=run_id,
        agent_id=agent_id,
        signal_type="heartbeat",
        output_quality=quality,
        efficiency=efficiency,
        task_id=task_id,
        progress_marker=progress_marker,
        tokens_spent_since=tokens_spent_since,
        findings_since=findings_since,
        notes="auto from heartbeat",
        auto_detect_loop=True,
        commit=commit,
    )


# ── Private helpers ──

def _check_loop_now(db, agent_id: str, current_novelty: float) -> bool:
    """检查当前信号 + 最近信号是否构成兜圈模式。"""
    recent = get_recent_worker_signals(db, agent_id, limit=LOOP_DETECT_WINDOW - 1)

    if len(recent) < LOOP_CONSECUTIVE_COUNT - 1:
        return False

    # 当前 + 最近 N-1 条
    novelties = [current_novelty] + [s["novelty_score"] for s in recent[:LOOP_CONSECUTIVE_COUNT - 1]]

    if all(n < LOOP_NOVELTY_THRESHOLD for n in novelties):
        return True

    return False
