"""Action-value scheduling for the swarm work market (opt-in).

Default scheduling ("priority" policy) is unchanged: role workers claim pending
market tasks ordered by the static, hand-assigned ``priority`` column
(``work_queue.poll_work_tasks``). When a run's config sets
``scheduler_policy = "value"``, this module re-ranks the run's pending tasks by a
learned, cost-aware action value *before* workers claim them, and records every
candidate decision to ``scheduler_decisions`` for offline value-vs-priority A/B.

The value model is a SQLite adaptation of the evidence-constrained action-value
policy validated in the reverse-engine swarm branch: interpretable fixed-weight
features in ``[0, 1]``, a Beta(1, 1) smoothed success probability, an explicit
exploration bonus/quota, and observed token cost. It learns from real market
outcomes (``agent_tasks``), not from model self-reports.

Design properties:

* **Opt-in / reversible.** ``maybe_rescore_pending`` is a no-op under the default
  policy, so existing runs schedule byte-for-byte identically.
* **Cold-start safe.** With no history, ``P(success)`` falls back to the Beta
  prior (0.5) and the score tracks the hand-assigned priority — behaviour stays
  close to today's static queue and only diverges as evidence accumulates.
* **Prior-preserving.** The original hand priority is captured once into
  ``agent_tasks.base_priority`` and kept as a feature, not discarded.
* **Auditable.** The new ordering is written back into ``priority`` (so the
  existing ``ORDER BY priority DESC`` transparently becomes value-ordered) and
  each candidate is logged with its features for measurement.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("swarm_knowledge.action_value")

POLICY_VERSION = "market-action-value-v1"
DEFAULT_POLICY = "priority"
DEFAULT_EXPLORATION_RATIO = 0.2

# A task whose historical mean cost reaches this many tokens maps to cost ~1.0.
COST_REFERENCE_TOKENS = 30_000.0
# Pending downstream children at/above this count map to unlock ~1.0.
UNLOCK_REFERENCE = 3.0
# How many recent finished tasks to aggregate the success/cost history from.
HISTORY_WINDOW = 2000
# Extra normalized value granted to an explore-quota task so it surfaces.
EXPLORE_BOOST = 0.15

# Fixed, interpretable weights. Kept explicit so ablation/tuning is a data task.
W_SUCCESS = 0.35   # learned success probability x designer prior
W_UNLOCK = 0.20    # real downstream fan-out unlocked by this task
W_COVERAGE = 0.15  # exploration coverage (prior unless a producer supplies it)
W_NOVELTY = 0.10   # anti-repeat within the run
W_PRIOR = 0.10     # designer prior as a standalone floor
W_COST = 0.10      # observed token cost (subtracted)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _clamp(value: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


def _load_json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def scheduler_policy_for_run(db, run_id: str) -> Tuple[str, float]:
    """Return ``(policy, exploration_ratio)`` from ``swarm_runs.config``.

    Unknown/absent config yields the safe default ("priority", 0.2).
    """
    row = db.fetch_one("SELECT config FROM swarm_runs WHERE run_id = ?", (run_id,))
    if not row:
        return DEFAULT_POLICY, DEFAULT_EXPLORATION_RATIO
    config = _load_json(row["config"])
    policy = str(config.get("scheduler_policy") or DEFAULT_POLICY).strip().lower()
    if policy not in {"priority", "value"}:
        policy = DEFAULT_POLICY
    ratio = _clamp(
        config.get("exploration_ratio", DEFAULT_EXPLORATION_RATIO),
        default=DEFAULT_EXPLORATION_RATIO,
    )
    return policy, ratio


def set_scheduler_policy(
    db,
    run_id: str,
    policy: str = "value",
    exploration_ratio: float = DEFAULT_EXPLORATION_RATIO,
) -> None:
    """Enable/disable value scheduling for a run by patching ``swarm_runs.config``.

    Convenience for gray-launch: ``set_scheduler_policy(db, run_id, "value")``.
    """
    if policy not in {"priority", "value"}:
        raise ValueError("policy must be 'priority' or 'value'")
    row = db.fetch_one("SELECT config FROM swarm_runs WHERE run_id = ?", (run_id,))
    if not row:
        raise ValueError(f"run not found: {run_id}")
    config = _load_json(row["config"])
    config["scheduler_policy"] = policy
    config["exploration_ratio"] = _clamp(exploration_ratio, default=DEFAULT_EXPLORATION_RATIO)
    db.execute(
        "UPDATE swarm_runs SET config = ? WHERE run_id = ?",
        (json.dumps(config, ensure_ascii=False), run_id),
    )
    db.conn.commit()


def signal_fingerprint(task: Dict[str, Any]) -> str:
    """Generalize history across equivalent tasks with different wording/context.

    Uses the durable, target-agnostic shape of a market task — role, task type,
    intent and knowledge type — so "exploit a vulnerability finding" shares a
    history bucket regardless of which specific finding triggered it.
    """
    focus = _load_json(task.get("focus_params"))
    role = str(task.get("required_role") or focus.get("required_role") or "").lower()
    task_type = str(task.get("task_type") or "").lower()
    intent = str(task.get("task_intent") or focus.get("knowledge_intent") or "").lower()
    ktype = str(focus.get("knowledge_type") or "").lower()
    return "|".join((role or "any", task_type or "any", intent or "any", ktype or "any"))


def _is_informative(result_summary: Any) -> bool:
    """A completed task is informative if it produced a durable artifact.

    The worker records ``captured_entry_id`` (a knowledge entry) or explicit
    findings/evidence in ``result_summary`` (see ``worker.py``). Completing with
    only prose and no captured artifact counts as non-informative — that is the
    correct learning signal: such a task type is not producing durable value.
    """
    data = _load_json(result_summary)
    if not data:
        return False
    for key in ("captured_entry_id", "finding_id", "entry_id"):
        if data.get(key):
            return True
    for key in ("findings", "evidence", "verified_findings"):
        value = data.get(key)
        if isinstance(value, (list, dict)) and len(value) > 0:
            return True
    return False


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #

@dataclass
class ActionHistory:
    """Aggregated outcomes for one signal fingerprint."""

    attempts: int = 0
    informative: int = 0
    total_tokens: float = 0.0
    cost_samples: int = 0

    @property
    def success_probability(self) -> float:
        # Beta(1, 1): one early result never becomes certainty.
        return (self.informative + 1.0) / (self.attempts + 2.0)

    @property
    def exploration_bonus(self) -> float:
        return 1.0 / math.sqrt(self.attempts + 1.0)

    @property
    def avg_tokens(self) -> float:
        return self.total_tokens / self.cost_samples if self.cost_samples else 0.0


def load_history(db, window: int = HISTORY_WINDOW) -> Dict[str, ActionHistory]:
    """Aggregate recent finished tasks into per-fingerprint history (global prior)."""
    rows = db.fetch_all(
        """SELECT task_type, required_role, task_intent, focus_params,
                  status, result_summary, token_cost
           FROM agent_tasks
           WHERE status IN ('completed', 'failed', 'timeout')
           ORDER BY created_at DESC
           LIMIT ?""",
        (int(window),),
    )
    history: Dict[str, ActionHistory] = {}
    for row in rows:
        task = dict(row)
        entry = history.setdefault(signal_fingerprint(task), ActionHistory())
        entry.attempts += 1
        if task.get("status") == "completed" and _is_informative(task.get("result_summary")):
            entry.informative += 1
        tokens = int(task.get("token_cost") or 0)
        if tokens > 0:
            entry.total_tokens += float(tokens)
            entry.cost_samples += 1
    return history


def _pending_children(db, run_id: str, task_id: str) -> int:
    row = db.fetch_one(
        "SELECT COUNT(*) AS c FROM agent_tasks "
        "WHERE run_id = ? AND parent_task_id = ? AND status = 'pending'",
        (run_id, task_id),
    )
    return int(row["c"]) if row else 0


def _run_repeats(db, run_id: str) -> Dict[str, int]:
    """Finished count per fingerprint within this run (drives novelty)."""
    rows = db.fetch_all(
        """SELECT task_type, required_role, task_intent, focus_params
           FROM agent_tasks
           WHERE run_id = ? AND status IN ('completed', 'failed', 'timeout')""",
        (run_id,),
    )
    repeats: Dict[str, int] = {}
    for row in rows:
        fingerprint = signal_fingerprint(dict(row))
        repeats[fingerprint] = repeats.get(fingerprint, 0) + 1
    return repeats


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

@dataclass
class ScoredTask:
    task_id: str
    fingerprint: str
    generation: int
    base_priority: int
    value_score: float
    features: Dict[str, float]
    attempts: int
    informative: int
    value_rank: int = 0
    mode: str = "exploit"
    effective_priority: int = 50


def _score_task(task: Dict[str, Any], history: Dict[str, ActionHistory],
                run_repeats: Dict[str, int], db, run_id: str) -> ScoredTask:
    fingerprint = signal_fingerprint(task)
    hist = history.get(fingerprint, ActionHistory())

    base_priority = task.get("base_priority")
    if base_priority is None:
        base_priority = task.get("priority")
    base_priority = int(base_priority if base_priority is not None else 50)
    base_prior = _clamp(base_priority / 100.0)

    success = hist.success_probability
    cost = _clamp(hist.avg_tokens / COST_REFERENCE_TOKENS) if hist.avg_tokens else 0.25
    unlock = _clamp(_pending_children(db, run_id, task["task_id"]) / UNLOCK_REFERENCE)
    novelty = 1.0 / (run_repeats.get(fingerprint, 0) + 1.0)
    focus = _load_json(task.get("focus_params"))
    coverage = _clamp(focus.get("coverage_gain"), default=0.15)

    value = (
        W_SUCCESS * success * base_prior
        + W_UNLOCK * unlock
        + W_COVERAGE * coverage
        + W_NOVELTY * novelty
        + W_PRIOR * base_prior
        - W_COST * cost
    )
    features = {
        "success_probability": round(success, 4),
        "base_prior": round(base_prior, 4),
        "unlock": round(unlock, 4),
        "coverage_gain": round(coverage, 4),
        "novelty": round(novelty, 4),
        "cost": round(cost, 4),
        "exploration_bonus": round(hist.exploration_bonus, 4),
    }
    return ScoredTask(
        task_id=task["task_id"],
        fingerprint=fingerprint,
        generation=max(1, int(task.get("iteration") or 1)),
        base_priority=base_priority,
        value_score=round(value, 6),
        features=features,
        attempts=hist.attempts,
        informative=hist.informative,
    )


def rescore_pending(
    db,
    run_id: str,
    exploration_ratio: float = DEFAULT_EXPLORATION_RATIO,
) -> Dict[str, Any]:
    """Re-rank a run's pending market tasks by action value and persist it.

    Writes the new ordering into ``agent_tasks.priority`` (existing claim path
    picks it up unchanged) and logs every candidate to ``scheduler_decisions``.
    """
    pending = [
        dict(r)
        for r in db.fetch_all(
            """SELECT task_id, run_id, parent_task_id, task_type, task_intent,
                      focus_params, required_role, priority, base_priority, iteration
               FROM agent_tasks
               WHERE run_id = ? AND status = 'pending'""",
            (run_id,),
        )
    ]
    if not pending:
        return {"rescored": 0, "explore": 0, "top": []}

    # Preserve the original hand priority once, before we overwrite `priority`.
    db.execute(
        "UPDATE agent_tasks SET base_priority = COALESCE(base_priority, priority) "
        "WHERE run_id = ? AND status = 'pending'",
        (run_id,),
    )
    db.conn.commit()
    for task in pending:
        if task.get("base_priority") is None:
            task["base_priority"] = task.get("priority")

    history = load_history(db)
    run_repeats = _run_repeats(db, run_id)
    scored = [_score_task(task, history, run_repeats, db, run_id) for task in pending]

    scored.sort(key=lambda item: (-item.value_score, item.task_id))
    for rank, item in enumerate(scored, start=1):
        item.value_rank = rank

    # Exploration quota: from the non-top set, surface the highest-uncertainty
    # tasks (exploration bonus + novelty) so the queue does not go purely greedy.
    count = len(scored)
    explore_count = int(count * exploration_ratio)
    if count > 1 and exploration_ratio > 0 and explore_count == 0:
        explore_count = 1
    exploit_cut = max(0, count - explore_count)
    explore_pool = sorted(
        scored[exploit_cut:],
        key=lambda item: (
            -(item.features["exploration_bonus"] + item.features["novelty"]),
            -item.value_score,
            item.task_id,
        ),
    )[:explore_count]
    explore_ids = {item.task_id for item in explore_pool}

    for item in scored:
        if item.task_id in explore_ids:
            item.mode = "explore"
            effective = _clamp(item.value_score + EXPLORE_BOOST)
        else:
            item.mode = "exploit"
            effective = _clamp(item.value_score)
        item.effective_priority = max(1, min(100, round(effective * 100)))
        db.execute(
            "UPDATE agent_tasks SET priority = ?, updated_at = datetime('now') "
            "WHERE task_id = ? AND status = 'pending'",
            (item.effective_priority, item.task_id),
        )
    db.conn.commit()

    _record_decisions(db, run_id, scored)
    return {
        "rescored": count,
        "explore": len(explore_ids),
        "top": [(item.task_id, item.effective_priority, item.mode) for item in scored[:5]],
    }


def _record_decisions(db, run_id: str, scored: List[ScoredTask]) -> None:
    """Log one decision per (run, task, generation); re-ticks replace in place."""
    for item in scored:
        db.execute(
            """INSERT OR REPLACE INTO scheduler_decisions
               (decision_id, run_id, task_id, generation, signal_fingerprint,
                policy, value_score, base_priority, effective_priority,
                value_rank, mode, features, attempts, informative)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), run_id, item.task_id, item.generation,
                item.fingerprint, POLICY_VERSION, item.value_score,
                item.base_priority, item.effective_priority, item.value_rank,
                item.mode, json.dumps(item.features, ensure_ascii=False),
                item.attempts, float(item.informative),
            ),
        )
    db.conn.commit()


def maybe_rescore_pending(db, run_id: str) -> Optional[Dict[str, Any]]:
    """Re-rank pending tasks by value iff the run opted into the value policy.

    Returns ``None`` under the default 'priority' policy (no DB writes), so
    scheduling for existing runs is unchanged. Any failure degrades gracefully
    to the static priority ordering already in the table.
    """
    policy, ratio = scheduler_policy_for_run(db, run_id)
    if policy != "value":
        return None
    try:
        return rescore_pending(db, run_id, exploration_ratio=ratio)
    except Exception:
        _log.exception("action-value rescore failed; keeping static priority ordering")
        return None
