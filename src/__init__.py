"""Swarm Knowledge Base — 蜂群智能体知识库 (SQLite)"""
__version__ = "0.3.0"
__description__ = "DIKW-powered knowledge base — capture from everything, use everywhere"

from .db import SwarmDB, get_db
from .governance.engine import (
    run_promotion_cycle, check_and_decay, cross_validate,
    build_similarity_graph, detect_communities_louvain, run_full_clustering,
    run_pheromone_decay, boost_pheromone, auto_distill_strategies,
)
from .governance.wisdom import distill_wisdom
from .governance.verification import auto_enqueue_validations, process_validation_queue
from .governance.tfidf_cluster import build_tfidf_similarity_graph, run_tfidf_clustering
from .ontology.inference import (
    discover_concepts_from_tasks, register_concepts, infer_transitive_relations,
    suggest_merges, detect_concept_drift, run_ontology_maintenance,
)
from .ontology.discovery import discover_relations_from_cooccurrence
from .agents.capture import (
    CaptureContext, CaptureSource, capture, capture_from_run, is_worth_capturing,
)
from .agents.retrieval import (
    search, search_by_tags, get_active_rules, get_similar,
    build_context_injection, select_best_strategy, query, knowledge_summary,
)
from .agents.extractor import (
    extract_knowledge_from_text, chunk_article, classify_knowledge_type,
)
from .swarm.spawn_handler import BaseSpawnHandler, MockSpawnHandler, HermesSpawnHandler

__all__ = [
    # DB
    "SwarmDB", "get_db",
    # Governance
    "run_promotion_cycle", "check_and_decay", "cross_validate",
    "build_similarity_graph", "detect_communities_louvain", "run_full_clustering",
    "run_pheromone_decay", "boost_pheromone", "auto_distill_strategies",
    # Wisdom
    "distill_wisdom",
    # Verification
    "auto_enqueue_validations", "process_validation_queue",
    # TF-IDF Clustering
    "build_tfidf_similarity_graph", "run_tfidf_clustering",
    # Ontology
    "discover_concepts_from_tasks", "register_concepts", "infer_transitive_relations",
    "suggest_merges", "detect_concept_drift", "run_ontology_maintenance",
    "discover_relations_from_cooccurrence",
    # Capture (write)
    "CaptureContext", "CaptureSource", "capture", "capture_from_run", "is_worth_capturing",
    # Retrieval (read)
    "search", "search_by_tags", "get_active_rules", "get_similar",
    "build_context_injection", "select_best_strategy", "query", "knowledge_summary",
    # Extractor (legacy)
    "extract_knowledge_from_text", "chunk_article", "classify_knowledge_type",
    # Spawn Handler
    "BaseSpawnHandler", "MockSpawnHandler", "HermesSpawnHandler",
]
