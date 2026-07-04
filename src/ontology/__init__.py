"""Swarm Knowledge Ontology — 本体推理引擎"""

from .inference import (
    discover_concepts_from_tasks,
    register_concepts,
    infer_transitive_relations,
    suggest_merges,
    detect_concept_drift,
    run_ontology_maintenance,
)
from .discovery import discover_relations_from_cooccurrence

__all__ = [
    "discover_concepts_from_tasks",
    "register_concepts",
    "infer_transitive_relations",
    "suggest_merges",
    "detect_concept_drift",
    "run_ontology_maintenance",
    "discover_relations_from_cooccurrence",
]
