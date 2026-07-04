"""
TF-IDF 语义聚类 — 替代纯 tag Jaccard 的聚类方法

传统方法只比较 tag 重叠，但 "sqli" vs "injection" vs "sql_injection"
如果 tag 不完全相同就会被漏掉。TF-IDF 直接从内容文本提取词频特征，
计算文档间的余弦相似度，能捕获语义相似性。

纯 Python 实现，不依赖 numpy/sklearn。适合 SQLite 单文件环境的约束。

用法:
    from src.governance.tfidf_cluster import build_tfidf_similarity_graph
    graph = build_tfidf_similarity_graph(db, threshold=0.15)
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Set, Tuple

_log = logging.getLogger("swarm_knowledge.tfidf")

# 中文停用词 + 英文停用词
STOPWORDS = {
    # 英文
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare",
    "ought", "used", "to", "of", "in", "for", "on", "with", "at", "by",
    "from", "as", "into", "through", "during", "before", "after", "above",
    "below", "up", "down", "out", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "all",
    "both", "each", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very", "s",
    "t", "can", "will", "just", "don", "should", "now", "this", "that",
    "these", "those", "it", "its", "they", "them", "their", "we", "us",
    "our", "you", "your", "he", "him", "his", "she", "her", "hers",
    # 通用技术词 (出现频率太高，无区分度)
    "http", "https", "www", "com", "org", "net", "html", "json", "text",
    "true", "false", "null", "none", "type", "name", "value", "key", "data",
    # 中文停用词
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都",
    "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会",
    "着", "没有", "看", "好", "自己", "这", "那", "里", "为", "可以",
    "对", "这个", "那个", "什么", "怎么", "这样", "那样", "如果", "因为",
    "所以", "但是", "不过", "然后", "或者", "还是", "虽然", "即使",
    "以下", "以上", "如下", "例如", "比如", "说明", "描述", "结果",
}

MIN_WORD_LEN = 2          # 最小词长
MAX_VOCAB_SIZE = 2000     # 最大词表大小 (防止过大)
MAX_DF_RATIO = 0.8        # 出现在 >80% 文档中的词过滤掉


def tokenize(text: str) -> List[str]:
    """分词: 英文按单词, 中文按字符 n-gram"""
    if not text:
        return []

    tokens = []
    # 英文单词
    en_words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{1,}', text.lower())
    tokens.extend(en_words)

    # 中文 bi-gram (2字符滑动窗口)
    cjk = re.findall(r'[\u4e00-\u9fff]+', text)
    for seg in cjk:
        if len(seg) <= 3:
            tokens.append(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.append(seg[i:i+2])

    # 过滤停用词 + 短词
    tokens = [t for t in tokens if t not in STOPWORDS and len(t) >= MIN_WORD_LEN]

    return tokens


def compute_tfidf(entries: List[Dict[str, str]]) -> Tuple[Dict[str, Dict[int, float]], Set[str]]:
    """
    计算每篇文档的 TF-IDF 向量。

    Args:
        entries: [{"id": ..., "content": ..., "tags": ...}, ...]

    Returns:
        (doc_vectors, vocabulary)
        doc_vectors: {entry_id: {term_idx: tfidf_value}}
        vocabulary: set of terms
    """
    N = len(entries)
    if N == 0:
        return {}, set()

    # 1. 分词 + 统计词频
    doc_tokens = {}  # entry_id -> Counter
    df = Counter()   # document frequency

    for entry in entries:
        # 合并 content + tags 作为文档
        tags = ""
        try:
            tag_list = json.loads(entry["tags"]) if isinstance(entry["tags"], str) else (entry["tags"] or [])
            tags = " ".join(tag_list)
        except (json.JSONDecodeError, TypeError):
            pass

        doc_text = (entry.get("content") or "") + " " + tags
        tokens = tokenize(doc_text)
        tc = Counter(tokens)
        doc_tokens[entry["id"]] = tc

        # 统计 df
        for term in tc:
            df[term] += 1

    # 2. 过滤高频词 (>MAX_DF_RATIO 的文档都包含的词)
    max_df = int(N * MAX_DF_RATIO)
    vocab = {term for term, count in df.items() if count <= max_df}

    # 限制词表大小
    if len(vocab) > MAX_VOCAB_SIZE:
        # 保留 df 适中的词 (不太常见也不太稀有)
        scored = [(t, df[t]) for t in vocab]
        scored.sort(key=lambda x: abs(x[1] - N // 2))  # 接近 N/2 的优先
        vocab = {t for t, _ in scored[:MAX_VOCAB_SIZE]}

    # 3. 计算 TF-IDF
    vocab_list = sorted(vocab)
    term_to_idx = {term: i for i, term in enumerate(vocab_list)}

    doc_vectors = {}
    for entry_id, tc in doc_tokens.items():
        vec = {}
        doc_len = sum(tc.values())
        if doc_len == 0:
            continue

        for term, count in tc.items():
            if term not in term_to_idx:
                continue
            tf = count / doc_len
            idf = math.log((N + 1) / (df[term] + 1)) + 1  # smoothed idf
            vec[term_to_idx[term]] = tf * idf

        # L2 归一化
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            vec = {k: v / norm for k, v in vec.items()}

        doc_vectors[entry_id] = vec

    return doc_vectors, vocab


def cosine_similarity_sparse(vec_a: Dict[int, float], vec_b: Dict[int, float]) -> float:
    """计算两个稀疏向量的余弦相似度 (已 L2 归一化，只需点积)"""
    # 交换较小的向量进行迭代
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    return sum(val * vec_b.get(idx, 0.0) for idx, val in vec_a.items())


def build_tfidf_similarity_graph(
    db,
    threshold: float = 0.15,
    limit: int = 500,
) -> Dict[str, Any]:
    """
    构建 TF-IDF 语义相似度图。

    Args:
        db: SwarmDB 实例
        threshold: 余弦相似度阈值 (0~1, 建议较低如 0.15 因为 TF-IDF 相似度通常偏低)
        limit: 最多处理多少条目

    Returns:
        {"entries": N, "links": M, "link_pairs": [(id_a, id_b, similarity), ...]}
    """
    rows = db.fetch_all(
        "SELECT id, content, tags FROM knowledge_entries WHERE status = 'active' AND level >= 1 ORDER BY created_at LIMIT ?",
        (limit,),
    )
    if len(rows) < 2:
        return {"entries": len(rows), "links": 0, "link_pairs": []}

    # 计算 TF-IDF
    doc_vectors, vocab = compute_tfidf([dict(r) for r in rows])

    if len(doc_vectors) < 2:
        return {"entries": len(rows), "links": 0, "link_pairs": []}

    # 计算两两相似度
    entry_ids = list(doc_vectors.keys())
    links = []

    for i in range(len(entry_ids)):
        for j in range(i + 1, len(entry_ids)):
            sim = cosine_similarity_sparse(doc_vectors[entry_ids[i]], doc_vectors[entry_ids[j]])
            if sim >= threshold:
                links.append((entry_ids[i], entry_ids[j], round(sim, 4)))

    _log.info("tfidf_graph: %d entries, %d vocab, %d links (threshold=%.2f)",
              len(entry_ids), len(vocab), len(links), threshold)
    return {"entries": len(entry_ids), "links": len(links), "link_pairs": links, "vocab_size": len(vocab)}


def run_tfidf_clustering(db, threshold: float = 0.15, limit: int = 500) -> Dict[str, Any]:
    """
    完整 TF-IDF 聚类流程: 计算 TF-IDF → 建图 → Louvain 社区检测。

    在 governance/engine.py 的 run_full_clustering 中可作为 build_similarity_graph 的替代。
    """
    from .engine import detect_communities_louvain

    graph = build_tfidf_similarity_graph(db, threshold, limit)
    result = {"phase": "tfidf_clustering", "graph": graph}

    if graph.get("link_pairs"):
        communities = detect_communities_louvain(db, graph["link_pairs"])
        result["communities"] = communities

    return result
