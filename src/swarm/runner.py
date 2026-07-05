"""
Automatic multi-worker swarm runner.

This is the local runtime counterpart to the work market: instead of manually
dispatching one phase at a time, a runner starts a role-sized worker pool and
lets workers claim tasks until the market is idle.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .orchestrator import SwarmOrchestrator
from .run_manager import default_role_counts
from .worker import Executor, SwarmWorker

ExecutorFactory = Callable[[str, str], Executor]


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


@dataclass
class RunnerResult:
    run_id: str
    workers: int
    processed: int
    rounds: int
    idle_rounds: int
    task_counts: Dict[str, int] = field(default_factory=dict)


class SwarmRunner:
    """Start multiple role workers for an existing swarm run."""

    def __init__(
        self,
        db,
        role_counts: Optional[Dict[str, int]] = None,
        poll_interval: float = 0.1,
        orchestrator: Optional[SwarmOrchestrator] = None,
    ):
        self.db = db
        self.role_counts = role_counts
        self.poll_interval = max(0.01, float(poll_interval))
        self.orchestrator = orchestrator or SwarmOrchestrator(db)

    def role_counts_for_run(self, run_id: str) -> Dict[str, int]:
        if self.role_counts:
            return {role: max(0, int(count)) for role, count in self.role_counts.items() if int(count) > 0}

        run = self.db.fetch_one("SELECT intent, config FROM swarm_runs WHERE run_id = ?", (run_id,))
        if not run:
            raise ValueError(f"run not found: {run_id}")
        config = _loads(run["config"], {})
        configured = config.get("min_agents_by_role")
        if isinstance(configured, dict) and configured:
            return {role: max(0, int(count)) for role, count in configured.items() if int(count) > 0}
        return default_role_counts(run["intent"], config.get("seed_profile", "balanced"))

    def build_workers(self, run_id: str, executor_factory: ExecutorFactory) -> List[SwarmWorker]:
        workers: List[SwarmWorker] = []
        for role, count in sorted(self.role_counts_for_run(run_id).items()):
            for idx in range(1, int(count) + 1):
                agent_id = f"{role}-{idx:02d}"
                executor = executor_factory(role, agent_id)
                workers.append(
                    SwarmWorker(
                        self.db,
                        run_id=run_id,
                        agent_id=agent_id,
                        role=role,
                        executor=executor,
                        poll_interval=self.poll_interval,
                    )
                )
        return workers

    async def run_until_idle(
        self,
        run_id: str,
        executor_factory: ExecutorFactory,
        max_rounds: int = 50,
        idle_round_limit: int = 2,
    ) -> RunnerResult:
        workers = self.build_workers(run_id, executor_factory)
        processed = 0
        idle_rounds = 0
        rounds = 0

        for rounds in range(1, max(1, int(max_rounds)) + 1):
            await self.orchestrator._tick_work_market(run_id)
            results = await asyncio.gather(*(w.run_once() for w in workers))
            completed_this_round = sum(1 for r in results if r is not None)
            processed += completed_this_round

            await self.orchestrator._tick_work_market(run_id)
            counts = self._task_counts(run_id)
            active = counts.get("pending", 0) + counts.get("running", 0)

            if completed_this_round == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0

            if active == 0 and idle_rounds >= idle_round_limit:
                break
            await asyncio.sleep(self.poll_interval)

        return RunnerResult(
            run_id=run_id,
            workers=len(workers),
            processed=processed,
            rounds=rounds,
            idle_rounds=idle_rounds,
            task_counts=self._task_counts(run_id),
        )

    def _task_counts(self, run_id: str) -> Dict[str, int]:
        rows = self.db.fetch_all(
            "SELECT status, COUNT(*) AS c FROM agent_tasks WHERE run_id = ? GROUP BY status",
            (run_id,),
        )
        return {r["status"]: int(r["c"]) for r in rows}


def adapt_executor_factory(factory: Callable[..., Executor]) -> ExecutorFactory:
    """Accept simple role-only factories while keeping role+agent_id available."""
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        params = {}

    def wrapper(role: str, agent_id: str) -> Executor:
        if len(params) <= 1:
            return factory(role)
        return factory(role, agent_id)

    return wrapper
