"""
独立验证 Pipeline — Orchestrator 自动验证 HIGH 发现

灵感来自 Anthropic Research 的多 agent 系统模式:
- 发现者 agent 提交发现
- 独立验证者 agent 重新检查证据
- 验证者只能更新 verified/confidence 字段, 不能创建新发现

本模块:
1. 自动将 vulnerability 类型 + 高置信度的知识加入验证队列
2. Orchestrator 定期 tick: 分配待验证条目给空闲 agent
3. 验证 agent 回写 verdict (confirmed/refuted/inconclusive)
4. confirmed → boost_pheromone + 更新 trust_vector
5. refuted → 降 trust + 记录 counter_example

用法 (Orchestrator 集成):
    from src.governance.verification import auto_enqueue_validations, process_validation_queue
    
    # 在 _tick_governance 中调用:
    auto_enqueue_validations(db)
    processed = process_validation_queue(db)
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from .bounty import create_finding_hypothesis
from .engine import boost_pheromone, compute_trust_score

_log = logging.getLogger("swarm_knowledge.verification")

# 验证阈值
VULN_TRUST_THRESHOLD = 0.65       # vulnerability 类型 knowledge 的 trust 需要达到此值才入队
VERIFICATION_BOOST = 0.25         # 验证 confirmed 时 pheromone boost
REFUTE_PENALTY = 0.20             # 验证 refuted 时 trust 下降
MAX_QUEUE_SIZE = 100               # 验证队列最大长度


def auto_enqueue_validations(db, run_id: str = None) -> Dict[str, Any]:
    """
    自动扫描知识库, 将需要验证的发现加入验证队列。

    触发条件:
    - knowledge_type = 'vulnerability' 且 trust >= 阈值 且未在队列中
    - knowledge_type = 'mechanism' 且 level >= 3 (高 level 机制需要验证)
    - 任何 level >= 3 且 validation_count == 0 的条目

    Returns:
        {"enqueued": N, "skipped": N}
    """
    run_filter = "AND ke.source_run_id = ?" if run_id else ""
    params: List[Any] = []
    if run_id:
        params.append(run_id)
    params.append(MAX_QUEUE_SIZE)

    # 找到需要验证的条目
    candidates = db.fetch_all(
        f"""SELECT ke.id, ke.title, ke.content, ke.knowledge_type,
                   ke.level, ke.trust_vector, ke.source_agent, ke.source_run_id
           FROM knowledge_entries ke
           WHERE ke.status = 'active'
             AND ke.validation_count = 0
             {run_filter}
             AND (
               (ke.knowledge_type = 'vulnerability' AND ke.level >= 1)
               OR (ke.knowledge_type = 'mechanism' AND ke.level >= 3)
               OR ke.level >= 3
             )
             AND ke.id NOT IN (
                 SELECT knowledge_id FROM validation_queue
                 WHERE status IN ('pending', 'assigned', 'validating', 'verified', 'refuted')
             )
           ORDER BY ke.level DESC, ke.created_at DESC LIMIT ?""",
        tuple(params),
    )

    enqueued = 0
    skipped = 0
    hypotheses = 0

    for entry in candidates:
        trust = compute_trust_score(entry["trust_vector"] or "{}")

        # vulnerability 需要达到 trust 阈值
        if entry["knowledge_type"] == "vulnerability" and trust < VULN_TRUST_THRESHOLD:
            skipped += 1
            continue

        # 计算 evidence hash (用于后续 replay 对比)
        evidence_hash = hashlib.sha256(
            (entry["content"][:500] + entry["knowledge_type"]).encode()
        ).hexdigest()[:16]

        validation_id = str(uuid.uuid4())
        priority = int(trust * 100) if trust > 0 else 50

        db.execute(
            """INSERT INTO validation_queue
               (validation_id, knowledge_id, run_id, requested_by,
                priority, evidence_hash, original_content, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                validation_id,
                entry["id"],
                entry["source_run_id"],
                "auto-verification",
                priority,
                evidence_hash,
                entry["content"][:1000],  # 快照
            ),
        )
        enqueued += 1

        if entry["knowledge_type"] == "vulnerability":
            created = create_finding_hypothesis(
                db,
                entry["id"],
                created_by="auto-verification",
                rationale="Auto-created when vulnerability entry entered validation_queue.",
            )
            if created:
                hypotheses += 1

    db.conn.commit()
    _log.info("verification_enqueue: %d enqueued, %d skipped, %d hypotheses",
              enqueued, skipped, hypotheses)
    return {"enqueued": enqueued, "skipped": skipped, "hypotheses": hypotheses}


def process_validation_queue(db) -> Dict[str, Any]:
    """
    处理验证队列中的待验证条目。

    在没有外部 agent 的情况下，执行自动验证逻辑:
    1. 重放 evidence: 检查原始内容是否包含可验证的特征
    2. 交叉验证: 检查其他 agent 是否独立报告了类似发现
    3. 反例检查: 是否有 counter_examples

    当有外部验证 agent 时, 此函数只做分配; 验证 agent 回写 verdict。
    """
    pending = db.fetch_all(
        """SELECT vq.validation_id, vq.knowledge_id, vq.original_content,
                  vq.evidence_hash, vq.priority, ke.knowledge_type, ke.trust_vector
           FROM validation_queue vq
           JOIN knowledge_entries ke ON vq.knowledge_id = ke.id
           WHERE vq.status = 'pending'
           ORDER BY vq.priority DESC LIMIT 20"""
    )

    if not pending:
        return {"processed": 0}

    processed = 0
    confirmed = 0
    refuted = 0
    inconclusive = 0

    for item in pending:
        vid = item["validation_id"]
        kid = item["knowledge_id"]

        # 自动验证: 交叉验证 + 反例检查
        verdict = _auto_verify(db, kid, item)

        # 写回 verdict
        db.execute(
            """UPDATE validation_queue
               SET status = ?, verdict = ?, verdict_reason = ?,
                   validated_at = datetime('now'), updated_at = datetime('now')
               WHERE validation_id = ?""",
            (
                "verified" if verdict["verdict"] == "confirmed" else
                "refuted" if verdict["verdict"] == "refuted" else "verified",
                verdict["verdict"],
                verdict["reason"],
                vid,
            ),
        )

        # 根据 verdict 更新知识条目
        if verdict["verdict"] == "confirmed":
            boost_pheromone(db, kid, VERIFICATION_BOOST)
            _update_trust(db, kid, delta=+0.05)
            confirmed += 1
        elif verdict["verdict"] == "refuted":
            _update_trust(db, kid, delta=-REFUTE_PENALTY)
            _mark_validation_attempt(db, kid)
            _record_counter_example(db, kid, verdict["reason"])
            refuted += 1
        else:
            _mark_validation_attempt(db, kid)
            inconclusive += 1

        processed += 1

    db.conn.commit()
    _log.info("verification_process: %d processed (confirmed=%d, refuted=%d, inconclusive=%d)",
              processed, confirmed, refuted, inconclusive)
    return {
        "processed": processed,
        "confirmed": confirmed,
        "refuted": refuted,
        "inconclusive": inconclusive,
    }


def _auto_verify(db, knowledge_id: str, item: dict) -> Dict[str, str]:
    """
    自动验证逻辑 (无需外部 agent)。

    三重检查:
    1. 是否有其他 agent 独立报告了类似发现 (lineage 中有 >1 个 source)
    2. 是否存在反例 (counter_examples)
    3. 原始内容是否包含可验证的特征 (URL/IP/CVE/命令输出)
    """
    reasons = []
    score = 0

    # 检查 1: 交叉验证
    lineage_count = db.fetch_one(
        """SELECT COUNT(DISTINCT source_type) AS c
           FROM knowledge_lineage
           WHERE knowledge_id = ? AND confidence_contribution > 0.5""",
        (knowledge_id,),
    )
    cross_sources = lineage_count["c"] if lineage_count else 0
    if cross_sources >= 2:
        score += 2
        reasons.append(f"有 {cross_sources} 个独立来源交叉验证")
    elif cross_sources == 1:
        score += 1
        reasons.append("有 1 个来源确认")
    else:
        reasons.append("无交叉验证来源")

    # 检查 2: 反例
    ce_count = db.fetch_one(
        "SELECT COUNT(*) AS c FROM counter_examples WHERE knowledge_id = ?",
        (knowledge_id,),
    )
    ce = ce_count["c"] if ce_count else 0
    if ce >= 3:
        score -= 3
        reasons.append(f"存在 {ce} 个反例")
    elif ce > 0:
        score -= 1
        reasons.append(f"存在 {ce} 个反例")

    # 检查 3: 可验证特征
    content = item["original_content"] if item["original_content"] else ""
    verifiable = False
    if re.search(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', content):
        verifiable = True
        reasons.append("包含 IP 地址")
        score += 1
    if re.search(r'CVE-\d{4}-\d+', content, re.IGNORECASE):
        verifiable = True
        reasons.append("包含 CVE 编号")
        score += 2
    if re.search(r'https?://\S+', content):
        verifiable = True
        reasons.append("包含 URL")
        score += 1
    if re.search(r'(nmap|nuclei|sqlmap|curl|jadx|ghidra|frida|burp)', content, re.IGNORECASE):
        verifiable = True
        reasons.append("包含工具输出特征")
        score += 1

    # 决定 verdict
    if score >= 3:
        return {"verdict": "confirmed", "reason": "; ".join(reasons)}
    elif score < 0:
        return {"verdict": "refuted", "reason": "; ".join(reasons)}
    else:
        return {"verdict": "inconclusive", "reason": "; ".join(reasons)}


def _update_trust(db, entry_id: str, delta: float) -> None:
    """更新 knowledge_entry 的 trust_vector.base_confidence"""
    row = db.fetch_one("SELECT trust_vector FROM knowledge_entries WHERE id = ?", (entry_id,))
    if not row:
        return
    tv = json.loads(row["trust_vector"] or "{}")
    tv["base_confidence"] = max(0.0, min(1.0, float(tv.get("base_confidence", 0.6)) + delta))
    tv["cross_validation"] = min(1.0, float(tv.get("cross_validation", 0.0)) + abs(delta) * 0.5)
    db.execute(
        "UPDATE knowledge_entries SET trust_vector = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(tv), entry_id),
    )


def _mark_validation_attempt(db, entry_id: str) -> None:
    """记录一次验证尝试，不改变置信度。"""
    db.execute(
        """UPDATE knowledge_entries
           SET last_validated_at = datetime('now'),
               validation_count = validation_count + 1,
               updated_at = datetime('now')
           WHERE id = ?""",
        (entry_id,),
    )


def _record_counter_example(db, knowledge_id: str, reason: str) -> None:
    """记录反例"""
    db.execute(
        """INSERT INTO counter_examples (id, knowledge_id, source_agent, description, severity)
           VALUES (?, ?, 'verification-pipeline', ?, 'moderate')""",
        (str(uuid.uuid4()), knowledge_id, f"验证管道反驳: {reason[:200]}"),
    )


# 需要 re
import re
