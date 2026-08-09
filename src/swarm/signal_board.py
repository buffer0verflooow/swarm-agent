"""
Shared Signal Board — graph-level blackboard for swarm coordination (P1).

Motivation (from the MARBLE database benchmark contrast experiment):
parallel workers that each re-scan the raw signal source lose context
(narrow field of view) and burn tokens. The fix that flipped the swarm from
4/7 to 8/8 exact was a *shared signal snapshot* collected once by a probe
node and read by every downstream worker, plus a lead that aggregates the
FULL evidence set. This module makes that pattern a first-class core
mechanism instead of benchmark-specific code.

Model
-----
A signal board is a JSON dict stored in ``task_graphs.metadata["signal_board"]``.
It is graph-scoped, append-oriented and versioned by node:

* **publish** — a probe/collector node writes structured signals once.
* **read**     — any worker claiming a graph-affiliated task gets the board
  injected into its context automatically (see ``build_signal_context``,
  wired into ``worker.build_task_context``).
* **evidence** — every completed node may attach evidence to the board; the
  lead/synthesize node reads the FULL evidence set (not a truncated digest)
  to make the final call.
* **immutable-after-write** — published signals are not overwritten by
  downstream nodes; only new keys or evidence entries may be appended. This
  keeps the board a trustworthy shared memory (no worker can rewrite another's
  finding).

Usage::

    from .signal_board import (
        publish_signal, get_signals, attach_evidence,
        collect_evidence, build_signal_context,
    )

    # collector node
    publish_signal(db, graph_id, "probe_snapshot", {"patterns": {...}})

    # worker side (automatic via build_task_context; explicit here)
    ctx = build_signal_context(db, task)          # injects board + goal

    # lead node
    evidence = collect_evidence(db, graph_id)     # full per-node evidence
    publish_signal(db, graph_id, "synthesis", {"final_roots": [...]})
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

_log = logging.getLogger("swarm_knowledge.signal_board")

SIGNAL_BOARD_KEY = "signal_board"
EVIDENCE_KEY = "evidence"
DEFAULT_MAX_CONTEXT_CHARS = 4000


def _loads_meta(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        obj = json.loads(value)
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _graph_meta(db, graph_id: str) -> Dict[str, Any]:
    row = db.fetch_one("SELECT metadata FROM task_graphs WHERE graph_id = ?", (graph_id,))
    return _loads_meta(row["metadata"] if row else None)


def _save_graph_meta(db, graph_id: str, meta: Dict[str, Any], commit: bool) -> None:
    db.execute(
        "UPDATE task_graphs SET metadata = ?, updated_at = datetime('now') WHERE graph_id = ?",
        (json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str), graph_id),
    )
    if commit:
        db.conn.commit()


def get_signals(db, graph_id: str) -> Dict[str, Any]:
    """Return the full signal board dict (signals only, no evidence)."""
    meta = _graph_meta(db, graph_id)
    board = meta.get(SIGNAL_BOARD_KEY, {})
    return board if isinstance(board, dict) else {}


def get_signal(db, graph_id: str, key: str, default: Any = None) -> Any:
    return get_signals(db, graph_id).get(key, default)


def publish_signal(
    db,
    graph_id: str,
    key: str,
    value: Any,
    overwrite: bool = False,
    commit: bool = True,
) -> None:
    """
    Publish a structured signal onto the board.

    Signals are append-only: an existing key is NOT overwritten unless
    ``overwrite=True`` (used by the collector itself on re-run). Downstream
    workers cannot clobber each other's published findings.
    """
    meta = _graph_meta(db, graph_id)
    board = meta.setdefault(SIGNAL_BOARD_KEY, {})
    if not isinstance(board, dict):
        board = {}
        meta[SIGNAL_BOARD_KEY] = board
    if key in board and not overwrite:
        _log.warning("signal '%s' already on board for graph %s (ignored)", key, graph_id)
        return
    board[key] = value
    _save_graph_meta(db, graph_id, meta, commit)


def attach_evidence(
    db,
    graph_id: str,
    node_key: str,
    evidence: Dict[str, Any],
    commit: bool = True,
) -> None:
    """
    Attach a node's evidence to the board (lead aggregation source).

    Evidence entries are keyed by node_key and never overwritten, so the
    lead always sees every worker's finding — full evidence, not a digest.
    """
    meta = _graph_meta(db, graph_id)
    board = meta.setdefault(SIGNAL_BOARD_KEY, {})
    if not isinstance(board, dict):
        board = {}
        meta[SIGNAL_BOARD_KEY] = board
    ev = board.setdefault(EVIDENCE_KEY, {})
    if not isinstance(ev, dict):
        ev = {}
        board[EVIDENCE_KEY] = ev
    ev[node_key] = evidence
    _save_graph_meta(db, graph_id, meta, commit)


def collect_evidence(db, graph_id: str) -> Dict[str, Dict[str, Any]]:
    """Return ALL evidence attached to the board (per node_key)."""
    ev = get_signals(db, graph_id).get(EVIDENCE_KEY, {})
    return ev if isinstance(ev, dict) else {}


def get_graph_id_for_task(db, task_id: str) -> Optional[str]:
    """Graph id for a market task (None if the task is not graph-affiliated)."""
    try:
        row = db.fetch_one("SELECT graph_id FROM agent_tasks WHERE task_id = ?", (task_id,))
        return row["graph_id"] if row and row["graph_id"] else None
    except Exception:
        return None


def build_signal_context(
    db,
    task: Dict[str, Any],
    max_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    include_evidence: bool = True,
) -> str:
    """
    Render the signal board into a compact context block for a claimed task.

    Injected automatically by ``worker.build_task_context`` for any
    graph-affiliated task: the worker sees the graph goal, the shared probe
    signals, and (optionally) evidence from sibling nodes — the shared
    context that lets narrow-scope workers reason globally.

    Returns "" when the task is not graph-affiliated (no-op for legacy flow).
    """
    task_id = task.get("task_id")
    graph_id = task.get("graph_id") or (get_graph_id_for_task(db, task_id) if task_id else None)
    if not graph_id:
        return ""
    try:
        row = db.fetch_one(
            "SELECT goal, status FROM task_graphs WHERE graph_id = ?", (graph_id,)
        )
    except Exception:
        row = None
    if not row:
        return ""

    parts = [f"## Shared Signal Board (graph {graph_id[:8]})"]
    if row["goal"]:
        parts.append(f"Graph goal: {row['goal'][:400]}")

    board = get_signals(db, graph_id)
    for key, value in board.items():
        if key == EVIDENCE_KEY and not include_evidence:
            continue
        if isinstance(value, dict):
            rendered = json.dumps(value, ensure_ascii=False, default=str)[:1200]
        else:
            rendered = str(value)[:1200]
        parts.append(f"[{key}] {rendered}")

    text = "\n\n".join(parts)
    return text[:max_chars]
