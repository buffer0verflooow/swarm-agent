"""
Swarm Knowledge Extraction — 知识提取管道

从用户发送的文章、论文、报告等内容中自动提取结构化知识，
分类、打标签、分配 DIKW 层级，写入知识库。

触发方式:
1. 用户发送文章 → 自动调用 extract_from_article()
2. 用户说 "提取知识" → 对当前对话内容调用 extract_from_text()
3. 批量导入 → extract_batch()

Design: 为蜂群知识库设计的入口管道
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_log = logging.getLogger("swarm_knowledge.extraction")

# ── 知识类型分类器 ──

KNOWLEDGE_TYPE_PATTERNS = {
    "vulnerability": [
        r"\b(CVE-\d{4}-\d{4,})\b",
        r"\b(vulnerability|漏洞|exploit|利用|overflow|溢出|injection|注入)\b",
        r"\b(RCE|LPE|DoS|XSS|CSRF|SSRF|IDOR|privilege escalation)\b",
    ],
    "technique": [
        r"\b(technique|技术|method|方法|approach|方案)\b",
        r"\b(bypass|绕过|hook|挂钩|inject|patch|打补丁)\b",
        r"\b(syscall|ROP|shellcode|payload)\b",
    ],
    "mechanism": [
        r"\b(mechanism|机制|principle|原理|workflow|流程)\b",
        r"\b(how it works|工作原理|内部实现)\b",
        r"\b(architecture|架构|design|设计)\b",
    ],
    "pattern": [
        r"\b(pattern|模式|trend|趋势|common|常见)\b",
        r"\b(typically|通常|generally|一般)\b",
        r"\b(best practice|最佳实践|经验)\b",
    ],
    "strategy": [
        r"\b(strategy|策略|approach|方法论|methodology)\b",
        r"\b(defense|防御|protection|保护|mitigation|缓解)\b",
    ],
    "tool_usage": [
        r"\b(tool|工具|usage|使用|command|命令)\b",
        r"\b(--?\w+|\.\/\w+)\b",  # CLI flags
    ],
    "observation": [
        r"\b(observe|观察|notice|注意|find|发现|discover)\b",
        r"\b(result|结果|outcome|output)\b",
    ],
    "fact": [
        r"\b(fact|事实|definition|定义|is a|是一种)\b",
        r"\b(according to|根据|per|按照)\b",
    ],
}

DOMAIN_PATTERNS = {
    "security": [r"\b(security|安全|vulnerability|CVE|exploit|attack)\b"],
    "reverse_engineering": [r"\b(reverse|逆向|disassemble|反汇编|decompile|binary|二进制)\b"],
    "web": [r"\b(web|HTTP|HTTPS|browser|前端|后端|API|REST|GraphQL)\b"],
    "network": [r"\b(network|网络|TCP|UDP|DNS|firewall|防火墙|proxy|代理)\b"],
    "system": [r"\b(kernel|内核|OS|操作系统|Linux|Windows|macOS|driver|驱动)\b"],
    "crypto": [r"\b(crypto|加密|decrypt|解密|cipher|hash|AES|RSA|ECC)\b"],
    "mobile": [r"\b(Android|iOS|APK|IPA|mobile|移动)\b"],
    "cloud": [r"\b(cloud|云|AWS|Azure|GCP|container|Docker|K8s)\b"],
    "ai_ml": [r"\b(AI|ML|LLM|model|模型|transformer|neural|神经网络)\b"],
    "iot": [r"\b(IoT|firmware|固件|embedded|嵌入式|SCADA|ICS)\b"],
}


@dataclass
class ExtractedKnowledge:
    """一条提取出的知识条目"""
    content: str
    title: str
    knowledge_type: str                        # observation/fact/mechanism/...
    domain: str                                # security/reverse_engineering/web/...
    level: int = 1                             # DIKW level (1=D, 2=I, 3=K, 4=W)
    tags: List[str] = field(default_factory=list)
    confidence: float = 0.75
    source_type: str = "document_extraction"
    source_ref: Dict[str, Any] = field(default_factory=dict)
    related_knowledge_ids: List[str] = field(default_factory=list)
    knowledge_intent: str = "understand"


def classify_knowledge_type(text: str) -> str:
    """基于文本内容自动分类 knowledge_type"""
    scores: Dict[str, int] = {}
    text_lower = text.lower()

    for ktype, patterns in KNOWLEDGE_TYPE_PATTERNS.items():
        score = 0
        for pattern in patterns:
            matches = re.findall(pattern, text_lower, re.IGNORECASE)
            score += len(matches)
        scores[ktype] = score

    if not scores or max(scores.values()) == 0:
        return "observation"

    return max(scores, key=scores.get)


def classify_domain(text: str) -> str:
    """基于文本内容自动分类领域"""
    scores: Dict[str, int] = {}
    text_lower = text.lower()

    for domain, patterns in DOMAIN_PATTERNS.items():
        score = 0
        for pattern in patterns:
            matches = re.findall(pattern, text_lower, re.IGNORECASE)
            score += len(matches)
        scores[domain] = score

    if not scores or max(scores.values()) == 0:
        return "general"

    return max(scores, key=scores.get)


def extract_tags(text: str, max_tags: int = 8) -> List[str]:
    """从文本中提取关键词标签"""
    tags: List[str] = []

    # Extract CVE IDs
    cves = re.findall(r'CVE-\d{4}-\d{4,}', text, re.IGNORECASE)
    tags.extend(cves[:3])

    # Extract tool names
    tool_patterns = [
        r'\b(nmap|nuclei|sqlmap|metasploit|burpsuite|hashcat|ffuf|hydra|impacket)\b',
        r'\b(ghidra|jadx|ida\s*pro|radare2|frida|x64dbg|ollydbg|windbg)\b',
        r'\b(bloodhound|cobalt\s*strike|sliver|havoc|mimikatz)\b',
        r'\b(wireshark|tcpdump|aircrack|john|hashcat|gobuster|dirb)\b',
    ]
    for pattern in tool_patterns:
        found = re.findall(pattern, text, re.IGNORECASE)
        tags.extend([t.lower().replace(' ', '_') for t in found])

    # Extract technique patterns
    technique_patterns = [
        r'\b(privilege escalation|priv_esc|PE)\b',
        r'\b(lateral movement|横向移动)\b',
        r'\b(persistence|持久化)\b',
        r'\b(command and control|C2)\b',
        r'\b(exfiltration|数据外泄)\b',
        r'\b(defense evasion|防御规避|免杀)\b',
        r'\b(initial access|初始访问)\b',
        r'\b(SQL injection|XSS|CSRF|SSRF|RCE|LFI|RFI)\b',
    ]
    for pattern in technique_patterns:
        found = re.findall(pattern, text, re.IGNORECASE)
        tags.extend([t.lower().replace(' ', '_') for t in found])

    # Deduplicate and limit
    seen = set()
    unique = []
    for t in tags:
        normalized = t.strip().lower()
        if normalized not in seen and len(unique) < max_tags:
            seen.add(normalized)
            unique.append(normalized)

    return unique


def estimate_dikw_level(
    text: str,
    has_references: bool = False,
    has_experimental_data: bool = False,
    has_cross_validation: bool = False,
) -> int:
    """
    估算 DIKW 层级

    Level 1 (Data): 纯观察/原始输出
    Level 2 (Information): 有结构的分析结果
    Level 3 (Knowledge): 有证据支撑的规律
    Level 4 (Wisdom): 可执行的元规则/方法论
    """
    score = 1.0

    # Structured analysis indicators
    if re.search(r'\b(because|因此|原因|due to|since)\b', text, re.IGNORECASE):
        score += 0.5
    if re.search(r'\b(evidence|证据|prove|证明|confirm|确认)\b', text, re.IGNORECASE):
        score += 0.5
    if len(text) > 500:
        score += 0.3

    if has_references:
        score += 0.5
    if has_experimental_data:
        score += 0.5
    if has_cross_validation:
        score += 0.5

    # Generalizable patterns
    if re.search(r'\b(generally|通常|in most cases|大多数|rule of thumb)\b', text, re.IGNORECASE):
        score += 0.8

    # Actionable directives
    if re.search(r'\b(should|应该|must|必须|recommend|建议|always|总是)\b', text, re.IGNORECASE):
        score += 1.0

    if score >= 3.5:
        return 4
    elif score >= 2.5:
        return 3
    elif score >= 1.8:
        return 2
    return 1


def compute_content_hash(content: str) -> str:
    """计算内容哈希用于去重"""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def chunk_article(text: str, max_chunk_chars: int = 2000) -> List[str]:
    """
    将长文章切分为可独立提取的知识块。
    按段落边界切分，尽量保持语义完整。
    """
    if len(text) <= max_chunk_chars:
        return [text]

    chunks = []
    paragraphs = text.split('\n\n')
    current = ""

    for para in paragraphs:
        if len(current) + len(para) > max_chunk_chars and current:
            chunks.append(current.strip())
            current = para
        else:
            current = current + '\n\n' + para if current else para

    if current.strip():
        chunks.append(current.strip())

    return chunks


def extract_title(text: str, max_len: int = 120) -> str:
    """从文本中提取或生成标题"""
    # Try to find a heading
    heading = re.search(r'^#+\s*(.+)$', text, re.MULTILINE)
    if heading:
        return heading.group(1).strip()[:max_len]

    # Use first non-empty line
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if lines:
        first = lines[0].lstrip('#').strip()
        if len(first) > 10:
            return first[:max_len]

    # Fallback: generate from content
    words = text.split()[:15]
    return ' '.join(words)[:max_len] + '...'


def extract_knowledge_from_text(
    text: str,
    source_url: Optional[str] = None,
    source_title: Optional[str] = None,
    source_agent: str = "knowledge-extractor",
    has_references: bool = False,
    has_experimental_data: bool = False,
    auto_dedup: bool = True,
) -> List[ExtractedKnowledge]:
    """
    从一段文本中提取结构化知识条目。

    Args:
        text: 原始文本内容
        source_url: 来源 URL
        source_title: 来源标题
        source_agent: 提取 Agent 标识
        has_references: 是否有引用/参考文献
        has_experimental_data: 是否有实验数据
        auto_dedup: 是否自动去重

    Returns:
        提取的知识条目列表
    """
    chunks = chunk_article(text)
    results: List[ExtractedKnowledge] = []
    seen_hashes: set = set() if auto_dedup else None

    for i, chunk in enumerate(chunks):
        content_hash = compute_content_hash(chunk)

        if auto_dedup and content_hash in seen_hashes:
            continue
        if auto_dedup:
            seen_hashes.add(content_hash)

        # 跳过纯代码块和太短的片段
        if chunk.count('```') >= 2 or len(chunk.strip()) < 80:
            continue

        knowledge_type = classify_knowledge_type(chunk)
        domain = classify_domain(chunk)
        tags = extract_tags(chunk)
        level = estimate_dikw_level(
            chunk,
            has_references=has_references,
            has_experimental_data=has_experimental_data,
        )
        title = extract_title(chunk)

        # 根据知识类型推断 knowledge_intent
        intent_map = {
            "vulnerability": "attack",
            "technique": "attack",
            "strategy": "defend",
            "mechanism": "understand",
            "pattern": "enumerate",
            "observation": "enumerate",
            "fact": "understand",
        }
        knowledge_intent = intent_map.get(knowledge_type, "understand")

        source_ref = {
            "content_hash": content_hash,
            "chunk_index": i,
            "extraction_method": "pattern_matching",
            "extraction_agent": source_agent,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        if source_url:
            source_ref["url"] = source_url
        if source_title:
            source_ref["title"] = source_title

        entry = ExtractedKnowledge(
            content=chunk,
            title=title,
            knowledge_type=knowledge_type,
            domain=domain,
            level=level,
            tags=tags,
            source_type="document_extraction",
            source_ref=source_ref,
            knowledge_intent=knowledge_intent,
        )
        results.append(entry)

    summary_domain = results[0].domain if results else "none"
    _log.info(
        "extract_knowledge: %d chunks → %d entries (domain=%s, types=%s)",
        len(chunks), len(results), summary_domain,
        ', '.join(sorted(set(e.knowledge_type for e in results))) if results else "none",
    )
    return results


def format_extraction_for_insert(
    entries: List[ExtractedKnowledge],
    run_id: Optional[str] = None,
    source_agent: str = "knowledge-extractor",
) -> List[Dict[str, Any]]:
    """
    将 ExtractedKnowledge 列表格式化为可直接 INSERT 的字典列表。

    返回格式与 knowledge_entries 表结构一致。
    """
    formatted = []
    for entry in entries:
        formatted.append({
            "id": str(uuid.uuid4()),
            "level": entry.level,
            "knowledge_type": entry.knowledge_type,
            "content": entry.content,
            "title": entry.title,
            "source_agent": source_agent,
            "source_run_id": run_id,
            "domain": entry.domain,
            "knowledge_intent": entry.knowledge_intent,
            "trust_vector": json.dumps({
                "logic_soundness": 0.6,
                "base_confidence": entry.confidence,
                "cross_validation": 0.0,
            }),
            "tags": entry.tags,
            "status": "active",
            "content_hash": compute_content_hash(entry.content),
            "source_ref": json.dumps(entry.source_ref, default=str),
        })
    return formatted


# ── 批量插入工具 ──

def _sql_literal(value: Any) -> str:
    """Return a SQLite string literal, or NULL."""
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return "'" + str(value).replace("'", "''") + "'"


def _json_text(value: Any, default: Any) -> str:
    """Normalize JSON-ish fields to text stored in SQLite."""
    if value is None:
        value = default
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, default=str)


def generate_insert_sql(entries: List[Dict[str, Any]]) -> str:
    """生成可在当前 SQLite schema 上执行的批量 INSERT SQL。"""
    if not entries:
        return "-- no entries to insert"

    values_clauses = []
    for e in entries:
        tags_json = _json_text(e.get("tags"), [])
        trust_vector_json = _json_text(e.get("trust_vector"), {
            "logic_soundness": 0.6,
            "base_confidence": 0.7,
            "cross_validation": 0.0,
        })
        content_hash = e.get("content_hash") or compute_content_hash(e["content"])
        values_clauses.append(
            f"""(
                {_sql_literal(e["id"])}, {int(e["level"])}, {_sql_literal(e["knowledge_type"])},
                {_sql_literal(e["content"])},
                {_sql_literal(e.get("title", ""))},
                {_sql_literal(e.get("source_agent", "knowledge-extractor"))},
                {_sql_literal(e.get("source_run_id"))},
                {_sql_literal(e.get("domain", "general"))},
                {_sql_literal(e.get("knowledge_intent", "understand"))},
                {_sql_literal(trust_vector_json)},
                {_sql_literal(tags_json)},
                {_sql_literal(e.get("status", "active"))},
                {_sql_literal(content_hash)}
            )"""
        )

    return f"""
    INSERT OR IGNORE INTO knowledge_entries (
        id, level, knowledge_type, content, title,
        source_agent, source_run_id, domain, knowledge_intent,
        trust_vector, tags, status, content_hash
    ) VALUES
    {','.join(values_clauses)};
    """


def generate_lineage_sql(
    entries: List[Dict[str, Any]],
    source_type: str = "document_extraction",
    extraction_method: str = "pattern_matching",
) -> str:
    """生成对应的 SQLite lineage INSERT SQL。"""
    if not entries:
        return "-- no lineage to insert"

    values_clauses = []
    for e in entries:
        source_ref_json = _json_text(e.get("source_ref"), {})
        values_clauses.append(
            f"""(
                {_sql_literal(e["id"])},
                {_sql_literal(source_type)},
                {_sql_literal(source_ref_json)},
                {_sql_literal(extraction_method)},
                1.0
            )"""
        )

    return f"""
    INSERT OR IGNORE INTO knowledge_lineage (
        knowledge_id, source_type, source_ref,
        extraction_method, confidence_contribution
    ) VALUES
    {','.join(values_clauses)};
    """
