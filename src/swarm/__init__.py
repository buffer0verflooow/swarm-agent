"""Swarm orchestration — lifecycle, spawn, orchestrator loop."""
from .lifecycle import AgentLifecycle, cleanup_stale_agents, get_live_agents
from .spawner import (
    request_spawn, poll_spawn_requests,
    mark_spawn_fulfilled, mark_spawn_rejected,
    expire_old_requests, merge_duplicate_requests,
)
from .orchestrator import SwarmOrchestrator, create_orchestrator

__all__ = [
    "AgentLifecycle", "cleanup_stale_agents", "get_live_agents",
    "request_spawn", "poll_spawn_requests",
    "mark_spawn_fulfilled", "mark_spawn_rejected",
    "expire_old_requests", "merge_duplicate_requests",
    "SwarmOrchestrator", "create_orchestrator",
]
