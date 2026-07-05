"""Swarm orchestration — lifecycle, spawn, orchestrator loop."""
from .lifecycle import AgentLifecycle, cleanup_stale_agents, get_live_agents
from .spawner import (
    request_spawn, poll_spawn_requests, claim_spawn_requests, recover_stale_spawn_claims,
    mark_spawn_fulfilled, mark_spawn_rejected,
    expire_old_requests, merge_duplicate_requests,
)
from .work_queue import (
    publish_work_task, publish_tasks_for_knowledge, poll_work_tasks,
    claim_work_tasks, recover_stale_work_claims,
    complete_work_task, fail_work_task,
)
from .model_config import (
    list_model_profiles, get_model_profile, upsert_model_profile,
    assign_task_model_profile, resolve_task_model_profile,
    record_swarm_event, build_run_summary,
)
from .artifacts import verify_artifact_path, verify_artifacts, record_artifact_verification
from .worker import SwarmWorker, build_task_context, normalize_executor_result
from .run_manager import (
    create_swarm_run, seed_swarm_run, create_seeded_swarm_run,
    build_seed_tasks, default_role_counts,
)
from .runner import SwarmRunner, RunnerResult, adapt_executor_factory
from .client_api import (
    submit_swarm_task, refresh_run_status, get_swarm_status,
    get_swarm_result, wait_for_swarm_result,
)
from .orchestrator import SwarmOrchestrator, create_orchestrator

__all__ = [
    "AgentLifecycle", "cleanup_stale_agents", "get_live_agents",
    "request_spawn", "poll_spawn_requests", "claim_spawn_requests", "recover_stale_spawn_claims",
    "mark_spawn_fulfilled", "mark_spawn_rejected",
    "expire_old_requests", "merge_duplicate_requests",
    "publish_work_task", "publish_tasks_for_knowledge", "poll_work_tasks",
    "claim_work_tasks", "recover_stale_work_claims",
    "complete_work_task", "fail_work_task",
    "list_model_profiles", "get_model_profile", "upsert_model_profile",
    "assign_task_model_profile", "resolve_task_model_profile",
    "record_swarm_event", "build_run_summary",
    "verify_artifact_path", "verify_artifacts", "record_artifact_verification",
    "SwarmWorker", "build_task_context", "normalize_executor_result",
    "create_swarm_run", "seed_swarm_run", "create_seeded_swarm_run",
    "build_seed_tasks", "default_role_counts",
    "SwarmRunner", "RunnerResult", "adapt_executor_factory",
    "submit_swarm_task", "refresh_run_status", "get_swarm_status",
    "get_swarm_result", "wait_for_swarm_result",
    "SwarmOrchestrator", "create_orchestrator",
]
