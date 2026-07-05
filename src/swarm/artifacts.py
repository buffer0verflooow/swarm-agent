"""
Artifact verification for swarm workers.

Sub-agent summaries are self-reports. A task is not allowed to claim that it
produced a file unless the parent runtime can see, stat, and hash that file.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]


def _json_text(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def _table_exists(db, table_name: str) -> bool:
    return bool(db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)))


def _ensure_artifact_schema(db) -> None:
    if not _table_exists(db, "agent_artifacts"):
        db.init()


def artifact_roots(extra_roots: Optional[Iterable[str]] = None) -> List[Path]:
    """Return roots where parent-side artifact verification is allowed."""
    raw_roots: List[str] = []
    env_roots = os.getenv("SWARM_ARTIFACT_ROOTS", "")
    if env_roots:
        raw_roots.extend(r for r in env_roots.split(os.pathsep) if r)
    if extra_roots:
        raw_roots.extend(str(r) for r in extra_roots if r)

    if not raw_roots:
        raw_roots = [
            str(Path.home() / "workspace"),
            str(REPO_ROOT),
            "/tmp",
        ]

    roots: List[Path] = []
    seen = set()
    for root in raw_roots:
        resolved = Path(root).expanduser().resolve()
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            roots.append(resolved)
    return roots


def _resolve_path(path: str, base_dir: Optional[str] = None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(base_dir or os.getcwd()) / p
    return p.resolve()


def _inside_any_root(path: Path, roots: List[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_artifact_spec(spec: Any) -> Dict[str, Any]:
    """Normalize string or dict artifact specs from worker/executor output."""
    if isinstance(spec, str):
        return {"path": spec, "required": True, "min_bytes": 1, "metadata": {}}
    if isinstance(spec, dict):
        return {
            "path": str(spec.get("path") or spec.get("file") or ""),
            "required": bool(spec.get("required", True)),
            "min_bytes": int(spec.get("min_bytes", 1) or 0),
            "metadata": spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {},
        }
    return {"path": "", "required": True, "min_bytes": 1, "metadata": {"invalid_spec": str(spec)}}


def verify_artifact_path(
    spec: Any,
    base_dir: Optional[str] = None,
    roots: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Verify one declared artifact from the parent-visible filesystem."""
    normalized = normalize_artifact_spec(spec)
    declared = normalized["path"]
    required = normalized["required"]
    min_bytes = max(0, int(normalized["min_bytes"]))
    metadata = normalized["metadata"]

    if not declared:
        return {
            "declared_path": declared,
            "resolved_path": "",
            "status": "missing",
            "ok": not required,
            "required": required,
            "size_bytes": 0,
            "sha256": "",
            "error": "empty artifact path",
            "metadata": metadata,
        }

    resolved = _resolve_path(declared, base_dir=base_dir)
    allowed_roots = artifact_roots(roots)
    if not _inside_any_root(resolved, allowed_roots):
        return {
            "declared_path": declared,
            "resolved_path": str(resolved),
            "status": "outside_root",
            "ok": not required,
            "required": required,
            "size_bytes": 0,
            "sha256": "",
            "error": "artifact path is outside allowed roots",
            "metadata": metadata,
        }

    if not resolved.exists():
        return {
            "declared_path": declared,
            "resolved_path": str(resolved),
            "status": "missing",
            "ok": not required,
            "required": required,
            "size_bytes": 0,
            "sha256": "",
            "error": "artifact is not visible to parent runtime",
            "metadata": metadata,
        }

    if not resolved.is_file():
        return {
            "declared_path": declared,
            "resolved_path": str(resolved),
            "status": "not_file",
            "ok": not required,
            "required": required,
            "size_bytes": 0,
            "sha256": "",
            "error": "artifact path is not a regular file",
            "metadata": metadata,
        }

    try:
        size = resolved.stat().st_size
        if size < min_bytes:
            return {
                "declared_path": declared,
                "resolved_path": str(resolved),
                "status": "empty",
                "ok": not required,
                "required": required,
                "size_bytes": size,
                "sha256": "",
                "error": f"artifact size {size} < required {min_bytes}",
                "metadata": metadata,
            }
        digest = _sha256(resolved)
    except OSError as exc:
        return {
            "declared_path": declared,
            "resolved_path": str(resolved),
            "status": "unreadable",
            "ok": not required,
            "required": required,
            "size_bytes": 0,
            "sha256": "",
            "error": str(exc),
            "metadata": metadata,
        }

    return {
        "declared_path": declared,
        "resolved_path": str(resolved),
        "status": "verified",
        "ok": True,
        "required": required,
        "size_bytes": size,
        "sha256": digest,
        "error": "",
        "metadata": metadata,
    }


def record_artifact_verification(
    db,
    run_id: str,
    task_id: str,
    agent_id: str,
    result: Dict[str, Any],
    commit: bool = True,
) -> str:
    _ensure_artifact_schema(db)
    artifact_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO agent_artifacts
           (artifact_id, run_id, task_id, agent_id, declared_path, resolved_path,
            status, size_bytes, sha256, required, error, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            artifact_id,
            run_id,
            task_id,
            agent_id,
            result["declared_path"],
            result["resolved_path"],
            result["status"],
            int(result.get("size_bytes") or 0),
            result.get("sha256") or "",
            1 if result.get("required", True) else 0,
            result.get("error") or "",
            _json_text(result.get("metadata") or {}),
        ),
    )
    if commit:
        db.conn.commit()
    return artifact_id


def verify_artifacts(
    db,
    run_id: str,
    task_id: str,
    agent_id: str,
    artifacts: Iterable[Any],
    base_dir: Optional[str] = None,
    roots: Optional[Iterable[str]] = None,
    commit: bool = True,
) -> Dict[str, Any]:
    """Verify and persist all declared artifacts for a task."""
    results: List[Dict[str, Any]] = []
    for spec in artifacts or []:
        result = verify_artifact_path(spec, base_dir=base_dir, roots=roots)
        result["artifact_id"] = record_artifact_verification(
            db,
            run_id=run_id,
            task_id=task_id,
            agent_id=agent_id,
            result=result,
            commit=False,
        )
        results.append(result)

    if commit and results:
        db.conn.commit()

    failed_required = [r for r in results if r.get("required", True) and not r["ok"]]
    return {
        "ok": not failed_required,
        "artifacts": results,
        "verified": [r for r in results if r["status"] == "verified"],
        "failed": failed_required,
    }
