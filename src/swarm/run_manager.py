"""
Swarm run manager.

This module starts a swarm run by seeding the shared work market, not by
building a fixed discover -> analyze -> exploit -> report chain.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from .model_config import resolve_task_model_profile
from .work_queue import publish_work_task


VALID_INTENTS = {"recon", "exploit", "analyze", "defend", "report", "custom"}
VALID_TARGET_TYPES = {"ip", "binary", "apk", "webapp", "domain", "network", "unknown"}
DEFAULT_MAX_AGENTS = 8


def default_role_counts(intent: str, profile: str = "balanced") -> Dict[str, int]:
    """Return conservative minimum worker counts for a run profile."""
    if intent in {"recon", "custom"}:
        if profile == "breadth":
            return {"scanner": 4, "analyst": 2, "exploiter": 1, "reporter": 1}
        if profile == "depth":
            return {"scanner": 1, "analyst": 3, "exploiter": 2, "reporter": 1}
        return {"scanner": 3, "analyst": 2, "exploiter": 1, "reporter": 1}
    if intent == "analyze":
        return {"analyst": 3, "reporter": 1}
    if intent == "exploit":
        return {"analyst": 2, "exploiter": 2, "reporter": 1}
    if intent == "defend":
        return {"analyst": 2, "reporter": 1}
    if intent == "report":
        return {"reporter": 2}
    return {"custom": 2, "reporter": 1}


def create_swarm_run(
    db,
    swarm_name: str,
    intent: str,
    target_type: str,
    target_id: str,
    config: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    commit: bool = True,
) -> str:
    """Create a swarm_runs row and return run_id."""
    if intent not in VALID_INTENTS:
        raise ValueError(f"invalid intent: {intent}")
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(f"invalid target_type: {target_type}")

    rid = run_id or str(uuid.uuid4())
    db.execute(
        """INSERT INTO swarm_runs
           (run_id, swarm_name, intent, target_type, target_id, status, config)
           VALUES (?, ?, ?, ?, ?, 'running', ?)""",
        (
            rid,
            swarm_name or f"swarm-{rid[:8]}",
            intent,
            target_type,
            target_id,
            json.dumps(config or {}, ensure_ascii=False, sort_keys=True, default=str),
        ),
    )
    if commit:
        db.conn.commit()
    return rid


def seed_swarm_run(
    db,
    run_id: str,
    intent: str,
    target_type: str,
    target_id: str,
    profile: str = "balanced",
    objective: str = "",
    commit: bool = True,
) -> List[Dict[str, Any]]:
    """
    Publish initial market tasks for a run.

    These are independent seed tasks with no parent_task_id. The orchestrator
    and live workers decide who claims them; discoveries later fan out into
    additional tasks through capture().
    """
    tasks = build_seed_tasks(intent, target_type, target_id, profile=profile)
    published: List[Dict[str, Any]] = []
    for task in tasks:
        reason = task["reason"]
        if objective:
            reason = f"{reason}\nClient objective: {objective}"
        task_id = publish_work_task(
            db,
            run_id=run_id,
            task_type=task["task_type"],
            required_role=task["required_role"],
            reason=reason,
            context_entry_ids=[],
            parent_task_id=None,
            source_agent="run-manager",
            intent=task.get("intent") or intent,
            priority=task["priority"],
            metadata={
                "target_type": target_type,
                "target_id": target_id,
                "seed_profile": profile,
                "market_source": "run_seed",
                "seed_name": task["name"],
                "client_objective": objective,
            },
            signal_key=f"seed:{task['name']}:{run_id}",
            commit=False,
        )
        stored = db.fetch_one("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,))
        model_profile = resolve_task_model_profile(db, dict(stored)) if stored else None
        published.append({**task, "task_id": task_id, "model_profile": model_profile})

    if commit and published:
        db.conn.commit()
    return published


def create_seeded_swarm_run(
    db,
    swarm_name: str,
    intent: str,
    target_type: str,
    target_id: str,
    profile: str = "balanced",
    config: Optional[Dict[str, Any]] = None,
    objective: str = "",
) -> Dict[str, Any]:
    """Create a run and seed the work market in one transaction."""
    role_counts = (config or {}).get("min_agents_by_role") or default_role_counts(intent, profile)
    max_agents = int((config or {}).get("max_agents") or DEFAULT_MAX_AGENTS)
    rid = create_swarm_run(
        db,
        swarm_name=swarm_name,
        intent=intent,
        target_type=target_type,
        target_id=target_id,
        config={
            "seed_profile": profile,
            "client_objective": objective,
            "min_agents_by_role": role_counts,
            "max_agents": max_agents,
            "generation": 1,
            **(config or {}),
        },
        commit=False,
    )
    tasks = seed_swarm_run(
        db,
        run_id=rid,
        intent=intent,
        target_type=target_type,
        target_id=target_id,
        profile=profile,
        objective=objective,
        commit=False,
    )
    db.conn.commit()
    return {"run_id": rid, "seeded_tasks": tasks, "min_agents_by_role": role_counts, "max_agents": max_agents}


def build_seed_tasks(
    intent: str,
    target_type: str,
    target_id: str,
    profile: str = "balanced",
) -> List[Dict[str, Any]]:
    """Return independent seed tasks for the shared work market."""
    target = f"{target_type}:{target_id}"
    tasks: List[Dict[str, Any]] = []

    def add(name: str, task_type: str, role: str, reason: str, priority: int, task_intent: str = ""):
        tasks.append({
            "name": name,
            "task_type": task_type,
            "required_role": role,
            "reason": reason,
            "priority": priority,
            "intent": task_intent or intent,
        })

    if intent in {"recon", "custom"}:
        add("scope-map", "scan", "scanner", f"Map in-scope surface for {target}", 85, "recon")
        add("service-fingerprint", "scan", "scanner", f"Fingerprint exposed services and technologies for {target}", 80, "recon")
        add("content-discovery", "scan", "scanner", f"Discover endpoints, parameters, and interesting paths for {target}", 75, "recon")
        add("initial-triage", "analyze", "analyst", f"Triage early signals and define promising hypotheses for {target}", 65, "analyze")
        add("rolling-report", "report", "reporter", f"Maintain a rolling report from confirmed knowledge for {target}", 45, "report")
    elif intent == "analyze":
        add("artifact-map", "analyze", "analyst", f"Map components, data flows, and trust boundaries for {target}", 85, "analyze")
        add("hypothesis-review", "analyze", "analyst", f"Generate and rank analysis hypotheses for {target}", 75, "analyze")
        add("rolling-report", "report", "reporter", f"Maintain analysis notes and confirmed findings for {target}", 50, "report")
    elif intent == "exploit":
        add("exploitability-review", "analyze", "analyst", f"Review known findings and preconditions before exploit validation for {target}", 85, "analyze")
        add("authorized-validation", "exploit", "exploiter", f"Validate authorized high-confidence findings for {target}", 80, "attack")
        add("impact-report", "report", "reporter", f"Maintain impact and reproduction notes for {target}", 60, "report")
    elif intent == "defend":
        add("control-review", "analyze", "analyst", f"Review defensive controls and gaps for {target}", 80, "defend")
        add("mitigation-report", "report", "reporter", f"Maintain mitigation plan and evidence for {target}", 70, "report")
    elif intent == "report":
        add("final-report", "report", "reporter", f"Build report from existing knowledge for {target}", 90, "report")

    if profile == "breadth" and intent in {"recon", "custom"}:
        add("asset-expansion", "scan", "scanner", f"Explore adjacent in-scope assets related to {target}", 70, "recon")
    elif profile == "depth" and intent in {"recon", "custom", "exploit"}:
        add("deep-triage", "analyze", "analyst", f"Deep-dive the highest-signal hypotheses for {target}", 72, "analyze")

    return tasks
