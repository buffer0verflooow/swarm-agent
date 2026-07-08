"""
Swarm Knowledge Capture — 统一知识捕获层

设计原则:
1. 知识来源不限于"文档提取"，而是系统运行中自然产生的任何有价值信息
2. 每次 Agent 交互都是潜在的知识源
3. 捕获是「环境式」的——不需要显式触发，随系统运行自动积累
4. 有信号/噪声阈值——不是所有东西都值得入库

知识来源:
- task_result:    Agent 任务执行结果
- user_correction: 用户纠正/反馈
- conversation:   对话中产生的洞见
- tool_output:    工具输出中的有价值信息
- error_resolution: 错误如何被解决的
- article:        用户发送的文章/文档 (传统提取)
- discovery:      系统自动发现的新模式

统一入口: CaptureContext → classify → enrich → store
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

_log = logging.getLogger("swarm_knowledge.capture")


# ============================================================================
# Capture Source Types
# ============================================================================

class CaptureSource(str, Enum):
    """知识捕获来源"""
    TASK_RESULT = "task_result"           # Agent 完成任务后的输出
    USER_CORRECTION = "user_correction"   # 用户纠正 Agent 的错误
    CONVERSATION = "conversation"         # 对话中提炼的洞见
    TOOL_OUTPUT = "tool_output"           # 工具执行结果
    ERROR_RESOLUTION = "error_resolution" # 错误被解决的方案
    ARTICLE = "article"                   # 用户发送的文章/文档
    DISCOVERY = "discovery"               # 系统自动发现的模式
    CROSS_VALIDATION = "cross_validation" # 多 Agent 交叉验证结果


HIGH_TRUST_SOURCES = (
    CaptureSource.TASK_RESULT, CaptureSource.USER_CORRECTION,
    CaptureSource.ERROR_RESOLUTION, CaptureSource.ARTICLE,
    CaptureSource.DISCOVERY, CaptureSource.CROSS_VALIDATION,
)


@dataclass
class CaptureContext:
    """统一的知识捕获上下文"""
    source: CaptureSource
    content: str                              # 原始内容
    source_agent: str = "system"              # 来源 Agent
    source_run_id: Optional[str] = None       # 关联的 swarm_run
    source_task_id: Optional[str] = None      # 关联的 task
    metadata: Dict[str, Any] = field(default_factory=dict)
    # metadata 示例:
    #   task_result: {"task_type": "scan", "tool": "nmap", "exit_code": 0}
    #   user_correction: {"original_statement": "...", "correction_type": "factual"}
    #   error_resolution: {"error_type": "timeout", "attempts": 3}
    #   article: {"url": "...", "title": "...", "format": "markdown"}


# ============================================================================
# Signal Detectors — 判断是否值得入库
# ============================================================================

def is_worth_capturing(ctx: CaptureContext) -> bool:
    """判断一条内容是否值得进入知识库"""
    return assess_capture_signal(ctx)["worth_capturing"]


def assess_capture_signal(ctx: CaptureContext) -> Dict[str, Any]:
    """Return the signal assessment used before promoting raw events to KB."""
    content = ctx.content.strip()
    if ctx.metadata.get("force_capture"):
        if ctx.source in HIGH_TRUST_SOURCES and (ctx.source_agent or "").strip():
            _log.warning(
                "capture: force_capture accepted source=%s source_agent=%s run_id=%s",
                ctx.source, ctx.source_agent, ctx.source_run_id,
            )
            return {
                "worth_capturing": True,
                "reason": "forced_capture",
                "signal_count": _count_signals(ctx),
                "min_signal": 0,
            }
        _log.warning(
            "capture: force_capture ignored source=%s source_agent=%s run_id=%s",
            ctx.source, ctx.source_agent, ctx.source_run_id,
        )
    # 高价值来源: 即使是短内容也信任 (agent 显式 capture 的发现)
    if len(content) < 60:
        if ctx.source in HIGH_TRUST_SOURCES:
            # 短但高价值 → 放行，但标记为低信号以便日后审查
            _log.info("capture: short content (%d chars) from trusted source %s — allowing", len(content), ctx.source)
        else:
            return {
                "worth_capturing": False,
                "reason": "content_too_short",
                "signal_count": _count_signals(ctx),
                "min_signal": 0,
            }
    if content.count('\n') < 1 and len(content) < 150:
        # 极短单行 → 大概率是闲聊，除非来源是高价值源
        if ctx.source not in HIGH_TRUST_SOURCES:
            return {
                "worth_capturing": False,
                "reason": "short_single_line_low_value_source",
                "signal_count": _count_signals(ctx),
                "min_signal": 1,
            }

    # 不同来源有不同的信号阈值
    thresholds = {
        CaptureSource.TASK_RESULT: 0,       # 任务结果总是有价值
        CaptureSource.USER_CORRECTION: 0,   # 用户纠正总是有价值
        CaptureSource.ERROR_RESOLUTION: 0,  # 错误解决总是有价值
        CaptureSource.ARTICLE: 0,           # 文章总是有价值
        CaptureSource.TOOL_OUTPUT: 1,       # 工具输出: 至少有一些结构化内容
        CaptureSource.CONVERSATION: 2,      # 对话: 需要强信号
        CaptureSource.DISCOVERY: 0,         # 系统发现总是有价值
        CaptureSource.CROSS_VALIDATION: 0,
    }

    min_signal = thresholds.get(ctx.source, 1)
    signals = _count_signals(ctx)
    worth = signals >= min_signal
    return {
        "worth_capturing": worth,
        "reason": "" if worth else "low_signal",
        "signal_count": signals,
        "min_signal": min_signal,
    }


def _count_signals(ctx: CaptureContext) -> int:
    """计算内容中的知识信号数量"""
    content = ctx.content.lower()
    signals = 0

    # 包含具体数值/数据
    if _has_numbers(content):
        signals += 1
    # 包含因果关系
    if any(w in content for w in [
        'because', 'therefore', 'thus', 'due to', 'since',
        '因此', '由于', '导致', '原因', '所以',
    ]):
        signals += 1
    # 包含工具/命令
    if any(w in content for w in [
        'nmap', 'curl', 'sqlmap', 'python', 'bash', './', '--', 'api',
        'frida', 'jadx', 'ghidra', 'ida', 'radare2',
    ]):
        signals += 1
    # 包含漏洞/CVE
    if 'cve-' in content or 'vulnerability' in content:
        signals += 2
    # 包含对比/判断
    if any(w in content for w in [
        'vs', 'compared', 'better', 'worse', 'recommend', 'suggest',
        '对比', '差异', '推荐', '建议',
    ]):
        signals += 1
    # 用户纠正信号
    if ctx.source == CaptureSource.USER_CORRECTION:
        signals += 2
    # 错误解决包含具体方案
    if ctx.source == CaptureSource.ERROR_RESOLUTION and any(
        w in content for w in ['fix', 'solution', 'solved', 'resolved', '解决', '方案', '修改', '改为', '换成']
    ):
        signals += 2

    return signals


def _has_numbers(text: str) -> bool:
    import re
    return bool(re.search(r'\d+', text))


def _table_exists(db, table_name: str) -> bool:
    return bool(db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)))


def _ensure_raw_event_schema(db) -> None:
    if not _table_exists(db, "raw_agent_events"):
        db.init()


def record_raw_agent_event(
    db,
    ctx: CaptureContext,
    signal_count: int = 0,
    capture_status: str = "received",
    filter_reason: str = "",
    knowledge_entry_id: Optional[str] = None,
    commit: bool = True,
) -> str:
    """Persist the unfiltered agent emission before any KB promotion decision."""
    _ensure_raw_event_schema(db)
    event_id = str(uuid.uuid4())
    content_hash = hashlib.sha256(ctx.content.encode()).hexdigest()[:16]
    db.execute(
        """INSERT INTO raw_agent_events
           (event_id, run_id, task_id, source_agent, source, content, content_hash,
            metadata, signal_count, capture_status, filter_reason, knowledge_entry_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            ctx.source_run_id,
            ctx.source_task_id,
            ctx.source_agent,
            ctx.source.value if isinstance(ctx.source, CaptureSource) else str(ctx.source),
            ctx.content,
            content_hash,
            json.dumps(ctx.metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
            int(signal_count or 0),
            capture_status,
            filter_reason,
            knowledge_entry_id,
        ),
    )
    if commit:
        db.conn.commit()
    return event_id


def update_raw_agent_event(
    db,
    event_id: Optional[str],
    capture_status: str,
    filter_reason: str = "",
    knowledge_entry_id: Optional[str] = None,
    commit: bool = True,
) -> None:
    if not event_id:
        return
    db.execute(
        """UPDATE raw_agent_events
           SET capture_status = ?,
               filter_reason = ?,
               knowledge_entry_id = COALESCE(?, knowledge_entry_id),
               updated_at = datetime('now')
           WHERE event_id = ?""",
        (capture_status, filter_reason, knowledge_entry_id, event_id),
    )
    if commit:
        db.conn.commit()


def _metadata_tags(metadata: Dict[str, Any]) -> List[str]:
    """Normalize explicit tags supplied by CLI or agent metadata."""
    raw_tags = metadata.get("tags", [])
    if isinstance(raw_tags, str):
        candidates = raw_tags.split(",")
    elif isinstance(raw_tags, (list, tuple, set)):
        candidates = raw_tags
    else:
        candidates = []

    tags = []
    seen = set()
    for tag in candidates:
        normalized = str(tag).strip().lower().replace(" ", "_")
        if normalized and normalized not in seen:
            seen.add(normalized)
            tags.append(normalized)
    return tags


def _merge_tags(*groups: List[str]) -> List[str]:
    merged = []
    seen = set()
    for group in groups:
        for tag in group:
            normalized = str(tag).strip().lower().replace(" ", "_")
            if normalized and normalized not in seen:
                seen.add(normalized)
                merged.append(normalized)
    return merged


# ============================================================================
# Classifier — DIKW 分级 + 领域 + 类型
# ============================================================================

VALID_KNOWLEDGE_INTENTS = {"understand", "attack", "defend", "enumerate", "optimize"}

INTENT_ALIASES = {
    "recon": "enumerate",
    "scan": "enumerate",
    "scanning": "enumerate",
    "enumeration": "enumerate",
    "discover": "enumerate",
    "discovery": "enumerate",
    "analyze": "understand",
    "analysis": "understand",
    "report": "understand",
    "custom": "understand",
    "exploit": "attack",
    "exploitation": "attack",
    "attack": "attack",
    "defense": "defend",
    "mitigate": "defend",
    "mitigation": "defend",
    "optimize": "optimize",
}


def _normalize_knowledge_intent(intent: str = None, default: str = "understand") -> str:
    value = str(intent or "").strip().lower().replace("-", "_")
    if value in VALID_KNOWLEDGE_INTENTS:
        return value
    return INTENT_ALIASES.get(value, default)


def classify_capture(ctx: CaptureContext) -> Dict[str, Any]:
    """
    对捕获内容进行自动分类。
    返回: {knowledge_type, domain, level, tags, knowledge_intent, confidence}
    """
    content = ctx.content

    # 知识类型 (与之前的 extractor 共用逻辑)
    from .extractor import classify_knowledge_type, classify_domain, extract_tags, estimate_dikw_level

    ktype = classify_knowledge_type(content)
    domain = classify_domain(content)
    tags = _merge_tags(_metadata_tags(ctx.metadata), extract_tags(content))
    intent = _infer_intent(ctx, ktype)

    # DIKW 分级: 不同来源有不同的基础 level
    source_min_level = {
        CaptureSource.TASK_RESULT: 1,        # D: 原始输出
        CaptureSource.TOOL_OUTPUT: 1,        # D: 原始输出
        CaptureSource.CONVERSATION: 1,       # D: 原始对话 → 需提升
        CaptureSource.USER_CORRECTION: 2,    # I: 纠正本身就包含分析
        CaptureSource.ERROR_RESOLUTION: 2,   # I: 解决方案
        CaptureSource.ARTICLE: 1,            # D: 原始文档
        CaptureSource.DISCOVERY: 2,          # I: 系统发现
        CaptureSource.CROSS_VALIDATION: 3,   # K: 跨验证的结论
    }

    base_level = source_min_level.get(ctx.source, 1)
    est_level = estimate_dikw_level(
        content,
        has_references=ctx.source == CaptureSource.ARTICLE,
        has_cross_validation=ctx.source == CaptureSource.CROSS_VALIDATION,
    )
    level = max(base_level, est_level)

    # 置信度: 来源决定基础置信度
    source_confidence = {
        CaptureSource.USER_CORRECTION: 0.90,
        CaptureSource.ERROR_RESOLUTION: 0.85,
        CaptureSource.CROSS_VALIDATION: 0.80,
        CaptureSource.DISCOVERY: 0.70,
        CaptureSource.ARTICLE: 0.65,
        CaptureSource.TASK_RESULT: 0.60,
        CaptureSource.TOOL_OUTPUT: 0.55,
        CaptureSource.CONVERSATION: 0.40,
    }
    confidence = source_confidence.get(ctx.source, 0.5)

    return {
        "knowledge_type": ktype,
        "domain": domain,
        "level": level,
        "tags": tags,
        "knowledge_intent": intent,
        "confidence": confidence,
    }


def _infer_intent(ctx: CaptureContext, ktype: str) -> str:
    """推断知识意图"""
    # 来源优先
    if ctx.source == CaptureSource.USER_CORRECTION:
        return "defend" if "不要" in ctx.content or "别" in ctx.content else "understand"
    if ctx.source == CaptureSource.ERROR_RESOLUTION:
        return "optimize"
    if ctx.source == CaptureSource.TASK_RESULT:
        return _normalize_knowledge_intent(ctx.metadata.get("intent"), "enumerate")

    # 类型推断
    intent_map = {
        "vulnerability": "attack",
        "technique": "attack",
        "strategy": "defend",
        "mechanism": "understand",
        "pattern": "enumerate",
        "observation": "enumerate",
        "fact": "understand",
    }
    return _normalize_knowledge_intent(intent_map.get(ktype), "understand")


# ============================================================================
# Enricher — 关联已有知识
# ============================================================================

def enrich_with_context(
    db,
    ctx: CaptureContext,
    classification: Dict[str, Any],
) -> Dict[str, Any]:
    """用已有知识丰富新条目"""
    domain = classification.get("domain", "")
    tags = classification.get("tags", [])

    # 找相关条目
    related = []
    if domain:
        rows = db.fetch_all(
            "SELECT id, title FROM knowledge_entries WHERE domain = ? AND status = 'active' LIMIT 5",
            (domain,),
        )
        related = [{"id": r["id"], "title": r["title"]} for r in rows]

    # 找同标签条目 → 可能形成 corroborating source
    corroborating = []
    if tags:
        for tag in tags[:3]:
            rows = db.fetch_all(
                "SELECT id, title FROM knowledge_entries WHERE tags LIKE ? AND status = 'active' LIMIT 3",
                (f'%"{tag}"%',),
            )
            corroborating.extend([{"id": r["id"], "title": r["title"]} for r in rows])

    # 如果是用户纠正 → 检查是否有冲突条目（潜在 counter_example）
    conflicts = []
    if ctx.source == CaptureSource.USER_CORRECTION:
        # FTS5 需要 sanitize: 取前3个有意义的关键词
        import re
        words = re.findall(r'[a-zA-Z_]{3,}', ctx.content[:200])
        ft_query = " OR ".join(words[:5]) if words else ctx.content[:80].replace("'", "")
        if ft_query:
            rows = db.fetch_all(
                "SELECT id, title FROM knowledge_entries WHERE level >= 2 AND status = 'active' "
                "AND id IN (SELECT rowid FROM knowledge_entries_fts WHERE knowledge_entries_fts MATCH ?) LIMIT 3",
                (ft_query,),
            )
            conflicts = [{"id": r["id"], "title": r["title"]} for r in rows]

    return {
        "related_entries": related,
        "corroborating_sources": corroborating,
        "potential_conflicts": conflicts,
    }


# ============================================================================
# Store — 统一入库
# ============================================================================

def capture(
    db,
    ctx: CaptureContext,
    auto_classify: bool = True,
) -> Optional[str]:
    """
    统一的知识捕获入口。

    使用方式（在 Agent 工作流中）:
        ctx = CaptureContext(
            source=CaptureSource.TASK_RESULT,
            content=nmap_output,
            source_agent="scanner-3",
            source_run_id=run_id,
            metadata={"task_type": "scan", "tool": "nmap"},
        )
        entry_id = capture(db, ctx)

    Returns:
        入库的 entry_id，如果被信号过滤器拒绝则返回 None
    """
    if ctx.metadata.get("force_capture") and (
        ctx.source not in HIGH_TRUST_SOURCES or not (ctx.source_agent or "").strip()
    ):
        ctx.metadata = dict(ctx.metadata)
        ctx.metadata.pop("force_capture", None)
        _log.warning(
            "capture: stripped unauthorized force_capture source=%s source_agent=%s run_id=%s",
            ctx.source, ctx.source_agent, ctx.source_run_id,
        )

    # 1. 无损原始事件记录。KB promotion 可以过滤，agent handoff 不能丢原文。
    assessment = assess_capture_signal(ctx)
    raw_event_id = record_raw_agent_event(
        db,
        ctx,
        signal_count=assessment["signal_count"],
        capture_status="received",
        filter_reason=assessment["reason"],
        commit=True,
    )

    # 2. 信号过滤
    if not assessment["worth_capturing"]:
        _log.debug("capture: filtered out (low signal) source=%s len=%d", ctx.source, len(ctx.content))
        update_raw_agent_event(
            db,
            raw_event_id,
            capture_status="filtered",
            filter_reason=assessment["reason"],
            commit=True,
        )
        return None

    # 3. 分类
    if auto_classify:
        classification = classify_capture(ctx)
    else:
        classification = {
            "knowledge_type": "observation",
            "domain": "general",
            "level": 1,
            "tags": [],
            "knowledge_intent": "understand",
            "confidence": 0.5,
        }

    # 4. 去重检查 — 用 content_hash 精确匹配
    content_hash = hashlib.sha256(ctx.content.encode()).hexdigest()[:16]
    existing = db.fetch_one(
        "SELECT id, level, trust_vector FROM knowledge_entries WHERE content_hash = ? AND status = 'active'",
        (content_hash,),
    )
    if existing:
        _log.debug("capture: duplicate detected, hash=%s existing=%s", content_hash, existing["id"][:8])
        # 不是完全拒绝，而是标记为 cross_validation 并提升已有条目
        classification["confidence"] = min(1.0, classification["confidence"] + 0.1)
        ctx.source = CaptureSource.CROSS_VALIDATION

        # 同时为已有条目增加一个 corroborating source
        db.execute(
            """INSERT OR IGNORE INTO knowledge_lineage
               (knowledge_id, source_type, source_ref, extraction_method, confidence_contribution)
               VALUES (?, ?, ?, 'cross_validation', ?)""",
            (
                existing["id"],
                "cross_agent_validation",
                json.dumps({"source_agent": ctx.source_agent, "content_hash": content_hash}, default=str),
                classification["confidence"],
            ),
        )
        db.execute(
            "UPDATE knowledge_entries SET last_validated_at = datetime('now'), validation_count = validation_count + 1, pheromone = 1.0 WHERE id = ?",
            (existing["id"],),
        )
        db.conn.commit()
        update_raw_agent_event(
            db,
            raw_event_id,
            capture_status="duplicate",
            filter_reason="duplicate_content_hash",
            knowledge_entry_id=existing["id"],
            commit=True,
        )
        return existing["id"]

    # 5. 关联已有知识
    enrichment = enrich_with_context(db, ctx, classification)

    # 6. 生成标题
    title = ctx.metadata.get("title", _generate_title(ctx, classification))

    # 7. 写入
    entry_id = str(uuid.uuid4())

    trust_vector = json.dumps({
        "logic_soundness": 0.6,
        "base_confidence": classification["confidence"],
        "cross_validation": 1.0 if enrichment["corroborating_sources"] else 0.0,
    })

    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent, source_run_id,
            source_task_id, domain, knowledge_intent, trust_vector, tags,
            content_hash, pheromone, last_validated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, datetime('now'))""",
        (
            entry_id,
            classification["level"],
            classification["knowledge_type"],
            ctx.content,
            title,
            ctx.source_agent,
            ctx.source_run_id,
            ctx.source_task_id,
            classification["domain"],
            classification["knowledge_intent"],
            trust_vector,
            json.dumps(classification["tags"]),
            content_hash,
        ),
    )

    # FTS 索引
    db.execute(
        "INSERT INTO knowledge_entries_fts(rowid, title, content) "
        "VALUES ((SELECT rowid FROM knowledge_entries WHERE id=?), ?, ?)",
        (entry_id, title, ctx.content[:500]),
    )

    # 8. 写入 lineage (source_type 需要映射)
    lineage_source_map = {
        CaptureSource.TASK_RESULT: "agent_execution",
        CaptureSource.USER_CORRECTION: "human_feedback",
        CaptureSource.ERROR_RESOLUTION: "agent_inference",
        CaptureSource.CONVERSATION: "agent_inference",
        CaptureSource.TOOL_OUTPUT: "agent_execution",
        CaptureSource.ARTICLE: "document_extraction",
        CaptureSource.DISCOVERY: "ontology_inference",
        CaptureSource.CROSS_VALIDATION: "cross_agent_validation",
    }
    lineage_source = lineage_source_map.get(ctx.source, "agent_execution")
    db.execute(
        """INSERT INTO knowledge_lineage
           (knowledge_id, source_type, source_ref, extraction_method, confidence_contribution)
           VALUES (?, ?, ?, 'agent_analysis', ?)""",
        (
            entry_id,
            lineage_source,
            json.dumps({
                "source_agent": ctx.source_agent,
                "run_id": ctx.source_run_id,
                "task_id": ctx.source_task_id,
                "content_hash": content_hash,
                **ctx.metadata,
            }, default=str),
            classification["confidence"],
        ),
    )

    # 9. 如果是用户纠正且存在冲突条目 → 自动记录 counter_example
    if ctx.source == CaptureSource.USER_CORRECTION and enrichment["potential_conflicts"]:
        for conflict in enrichment["potential_conflicts"]:
            db.execute(
                """INSERT OR IGNORE INTO counter_examples
                   (id, knowledge_id, source_agent, description, severity)
                   VALUES (?, ?, ?, ?, 'moderate')""",
                (
                    str(uuid.uuid4()),
                    conflict["id"],
                    ctx.source_agent,
                    f"用户纠正: {title[:200]}",
                ),
            )

    # 9.5 发布蜂群任务市场信号，并为缺少容量的角色触发 spawn
    _emit_swarm_signals(db, entry_id, classification, ctx)

    # 9.6 token 估算 — 基于内容长度估算 token 消耗
    estimated_tokens = _estimate_tokens(ctx.content, classification)
    if ctx.source_run_id and ctx.source_task_id:
        db.execute(
            "UPDATE agent_tasks SET estimated_tokens = COALESCE(estimated_tokens, 0) + ?, token_cost = COALESCE(token_cost, 0) + ? WHERE task_id = ?",
            (estimated_tokens, estimated_tokens, ctx.source_task_id),
        )
        db.execute(
            "UPDATE swarm_runs SET tokens_spent = COALESCE(tokens_spent, 0) + ? WHERE run_id = ?",
            (estimated_tokens, ctx.source_run_id),
        )

    # 9.7 自动 corroborating — 同 domain + 同 tags 的已有条目自动建立 lineage 关联
    _auto_corroborate(db, entry_id, classification)

    db.conn.commit()
    update_raw_agent_event(
        db,
        raw_event_id,
        capture_status="captured",
        filter_reason="",
        knowledge_entry_id=entry_id,
        commit=True,
    )

    _log.info(
        "capture: stored entry_id=%s source=%s type=%s level=L%d domain=%s",
        entry_id[:8], ctx.source.value, classification["knowledge_type"],
        classification["level"], classification["domain"],
    )

    return entry_id


def _emit_swarm_signals(db, entry_id: str, classification: Dict[str, Any], ctx: CaptureContext):
    """
    发现高价值目标时向共享任务市场发布工作单元。

    一个发现可以同时生成 analyze/exploit/report 等多个任务。已有 live
    agent 会从市场抢占任务；如果某个角色容量不足，再留下 spawn 信号。
    """
    high_value_sources = {CaptureSource.TASK_RESULT, CaptureSource.DISCOVERY, CaptureSource.CROSS_VALIDATION}
    if ctx.source not in high_value_sources:
        return

    run_id = ctx.source_run_id
    if not run_id:
        return

    try:
        from ...swarm.work_queue import publish_tasks_for_knowledge
    except ImportError:
        from src.swarm.work_queue import publish_tasks_for_knowledge

    tasks = publish_tasks_for_knowledge(
        db,
        entry_id=entry_id,
        classification=classification,
        run_id=run_id,
        source_agent=ctx.source_agent,
        parent_task_id=ctx.source_task_id,
        commit=False,
    )
    if not tasks:
        return

    _maybe_request_spawn_for_market_tasks(db, entry_id, classification, ctx, tasks)
    _log.info(
        "_emit_swarm_signals: published %d market tasks from %s",
        len(tasks), entry_id[:8],
    )


def _maybe_request_spawn_for_market_tasks(
    db,
    entry_id: str,
    classification: Dict[str, Any],
    ctx: CaptureContext,
    tasks: List[Dict[str, Any]],
):
    """为任务市场中容量不足的角色留下 spawn 信号。"""
    try:
        from ...swarm.spawner import request_spawn
    except ImportError:
        from src.swarm.spawner import request_spawn

    run_id = ctx.source_run_id
    if not run_id:
        return

    # 链深度: 从父 spawn 请求继承
    parent_chain_depth = 0
    if ctx.source_task_id:
        parent_req = db.fetch_one(
            "SELECT chain_depth FROM spawn_requests WHERE spawned_agent_id = ? OR parent_task_id = ? ORDER BY created_at DESC LIMIT 1",
            (ctx.source_agent, ctx.source_task_id),
        )
        if parent_req:
            parent_chain_depth = parent_req["chain_depth"] or 0

    for role in sorted({t["required_role"] for t in tasks}):
        pending_tasks = db.fetch_one(
            """SELECT COUNT(*) AS c FROM agent_tasks
               WHERE run_id = ? AND status = 'pending' AND required_role = ?""",
            (run_id, role),
        )["c"]
        live_agents = db.fetch_one(
            """SELECT COUNT(*) AS c
               FROM agent_heartbeats ah
               JOIN agent_profiles ap ON ah.agent_id = ap.agent_id
               WHERE ah.run_id = ? AND ap.role = ?
                 AND (julianday('now') - julianday(ah.last_beat)) * 86400 < 90""",
            (run_id, role),
        )["c"]
        active_spawn = db.fetch_one(
            """SELECT COUNT(*) AS c FROM spawn_requests
               WHERE run_id = ? AND requested_role = ?
                 AND status IN ('pending', 'spawning')""",
            (run_id, role),
        )["c"]

        if pending_tasks <= live_agents + active_spawn:
            continue

        max_priority = max(t["priority"] for t in tasks if t["required_role"] == role)
        request_spawn(
            db,
            run_id=run_id,
            requesting_agent=ctx.source_agent,
            requested_role=role,
            reason=(
                f"任务市场需要 {role}: {pending_tasks} 个待处理任务，"
                f"来自 {classification.get('knowledge_type')} [{entry_id[:8]}]"
            ),
            context_entry_ids=[entry_id],
            parent_task_id=ctx.source_task_id,
            priority=max_priority,
            chain_depth=parent_chain_depth + 1,
            commit=False,
        )
        _log.info(
            "_emit_swarm_signals: requested %s capacity for %s",
            role, entry_id[:8],
        )


def _estimate_tokens(content: str, classification: dict) -> int:
    """估算一条知识捕获消耗的 token 数。
    
    粗略规则: 1 token ≈ 4 chars (英文) / 1.5 chars (中文)
    知识条目本身 + 检索上下文 + 分类推理开销
    """
    content_len = len(content)
    # 混合中英文估算
    cjk_count = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
    ascii_count = content_len - cjk_count
    content_tokens = int(cjk_count / 1.5 + ascii_count / 4)
    # 加上检索/分类/推理开销 (约 2x 内容 token)
    return content_tokens * 3


def _auto_corroborate(db, entry_id: str, classification: dict):
    """自动为同 domain + 共享 tags 的已有条目建立 corroborating lineage。
    
    这解决了 DIKW 提升瓶颈: 之前 corroborating 只靠内容哈希去重触发，
    但不同 agent 用不同措辞描述同一现象时不会触发。
    现在通过 domain + tag 语义匹配自动建立关联。
    """
    domain = classification.get("domain", "")
    tags = classification.get("tags", [])
    if not domain and not tags:
        return

    # 找同 domain + 共享至少 1 个 tag 的已有条目
    conditions = ["ke.id != ?", "ke.status = 'active'", "ke.domain = ?"]
    params = [entry_id, domain]
    
    if tags:
        tag_conditions = " OR ".join(["ke.tags LIKE ?" for _ in tags[:3]])
        conditions.append(f"({tag_conditions})")
        for tag in tags[:3]:
            params.append(f'%"{tag}"%')
    
    sql = f"""SELECT ke.id, ke.level FROM knowledge_entries ke
              WHERE {" AND ".join(conditions)}
              ORDER BY ke.level DESC LIMIT 5"""
    
    related = db.fetch_all(sql, tuple(params))
    for r in related:
        related_id = r["id"]
        # 写入 corroborating lineage (如果不存在)
        db.execute(
            """INSERT OR IGNORE INTO knowledge_lineage
               (knowledge_id, source_type, source_ref, extraction_method, confidence_contribution)
               VALUES (?, 'cross_agent_validation', ?, 'pattern_matching', 0.6)""",
            (
                related_id,
                json.dumps({"corroborated_by": entry_id, "method": "domain_tag_match"}),
            ),
        )



def _generate_title(ctx: CaptureContext, classification: Dict[str, Any]) -> str:
    """根据来源自动生成标题"""
    prefix_map = {
        CaptureSource.TASK_RESULT: "[任务]",
        CaptureSource.USER_CORRECTION: "[纠正]",
        CaptureSource.CONVERSATION: "[对话]",
        CaptureSource.TOOL_OUTPUT: "[工具]",
        CaptureSource.ERROR_RESOLUTION: "[错误]",
        CaptureSource.ARTICLE: "[文章]",
        CaptureSource.DISCOVERY: "[发现]",
        CaptureSource.CROSS_VALIDATION: "[交叉验证]",
    }
    prefix = prefix_map.get(ctx.source, "")

    # 从内容取前80字符作为标题
    first_line = ctx.content.strip().split('\n')[0]
    title_text = first_line[:80]
    if len(first_line) > 80:
        title_text += "..."

    return f"{prefix} {title_text}" if prefix else title_text


# ============================================================================
# Batch — 批量捕获 (定时任务)
# ============================================================================

def capture_from_run(db, run_id: str, min_signal: int = 1) -> Dict[str, Any]:
    """从一次 swarm_run 中批量捕获所有有价值的知识"""
    # 捕获所有完成的 task 结果
    tasks = db.fetch_all(
        "SELECT task_id, task_type, focus_params, result_summary, agent_id, status "
        "FROM agent_tasks WHERE run_id = ? AND status = 'completed' AND result_summary != '{}'",
        (run_id,),
    )

    captured = []
    filtered = 0
    for task in tasks:
        try:
            result = json.loads(task["result_summary"]) if isinstance(task["result_summary"], str) else task["result_summary"]
        except (json.JSONDecodeError, TypeError):
            result = {}

        # 提取有意义的文本内容
        content_parts = []
        for key in ("output", "summary", "findings", "result", "error"):
            if key in result and result[key]:
                content_parts.append(str(result[key]))
        content = "\n".join(content_parts)

        if not content.strip():
            continue

        ctx = CaptureContext(
            source=CaptureSource.TASK_RESULT,
            content=content,
            source_agent=task["agent_id"] or "unknown",
            source_run_id=run_id,
            source_task_id=task["task_id"],
            metadata={
                "task_type": task["task_type"],
                "focus_params": task["focus_params"],
            },
        )

        entry_id = capture(db, ctx)
        if entry_id:
            captured.append(entry_id)
        else:
            filtered += 1

    return {"run_id": run_id, "captured": len(captured), "filtered": filtered}
