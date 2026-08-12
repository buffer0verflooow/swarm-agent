"""
Swarm-owned skill registry (Hermes-independent).

The swarm loads skill CONTENT — not just names — into worker context. Skills
are plain markdown files under ``<repo>/skills/`` (override with the
``SWARM_SKILLS_DIR`` environment variable) with optional YAML-ish frontmatter:

    ---
    name: analyst
    description: 静态分析方法论 — 数据流/边界检查/证据分级
    tags: [static-analysis, binary, evidence]
    ---
    <body: 具体方法论、检查清单、证据标准>

Why content-level (2026-08-12):

- migration 008 (2026-08-11) only injected ``load_skills`` NAME strings as
  bullet points; the real content depended on the external executor (Hermes
  ``--skills``). The 2026-08-11 ablation showed the injection pipeline works
  but methodology sentences had zero measurable effect — the fix is real,
  editable content files (domain-fact style), not a name list.
- ``load_skills`` entries resolve in order: exact skill name -> case-insensitive
  name -> tag match -> legacy passthrough (the entry is injected verbatim as a
  plain instruction, preserving migration 008 behaviour for unresolvable refs).

Ablation switch ``SWARM_SKILL_PACKS=0`` (used by benchmark baselines) disables
injection entirely — the resolver is skipped and nothing is appended.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent.parent

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# 注入预算: 每个 worker 上下文里技能内容的总字符上限 (env 可调)。
DEFAULT_SKILL_BUDGET_CHARS = 6000
# 单个技能块上限 (防止一个超长技能吃光整份预算)。
DEFAULT_SKILL_BLOCK_CHARS = 3000


def default_skills_dir() -> Path:
    """Skills directory: $SWARM_SKILLS_DIR or <repo>/skills."""
    env = os.environ.get("SWARM_SKILLS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return REPO / "skills"


def parse_frontmatter(text: str) -> Dict[str, Any]:
    """Parse leading ``---`` frontmatter into a dict; returns {} when absent.

    Handles ``key: value``, ``key: [a, b]`` (list), ``tags: [x, y]`` style.
    Unknown keys are preserved as raw strings.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    meta: Dict[str, Any] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [i.strip().strip("'\"") for i in inner.split(",") if i.strip()]
            meta[key] = items
        elif value.lower() in ("true", "false"):
            meta[key] = value.lower() == "true"
        else:
            meta[key] = value.strip("'\"")
    return meta


def discover_skills(skills_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List all skills in the skills directory (sorted by name)."""
    root = Path(skills_dir) if skills_dir else default_skills_dir()
    found: List[Dict[str, Any]] = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = parse_frontmatter(text)
        name = str(meta.get("name") or path.stem).strip()
        found.append(
            {
                "name": name,
                "path": str(path),
                "description": str(meta.get("description") or ""),
                "tags": meta.get("tags") or [],
                "size_chars": len(text),
            }
        )
    return found


def load_skill(name: str, skills_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load one skill by name; returns None when missing/unreadable."""
    root = Path(skills_dir) if skills_dir else default_skills_dir()
    path = root / f"{name}.md"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta = parse_frontmatter(text)
    body = FRONTMATTER_RE.sub("", text).strip()
    resolved_name = str(meta.get("name") or path.stem).strip()
    return {
        "name": resolved_name,
        "path": str(path),
        "description": str(meta.get("description") or ""),
        "tags": meta.get("tags") or [],
        "body": body,
    }


def _tag_matches(entry: str, skill: Dict[str, Any]) -> bool:
    needle = entry.strip().lower().rstrip(".")
    if not needle:
        return False
    for tag in skill.get("tags") or []:
        if str(tag).strip().lower() == needle:
            return True
    return False


def resolve_skill_ref(ref: str, skills_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Resolve a load_skills entry to a skill file.

    Order: exact name -> case-insensitive name -> tag match. Returns None when
    nothing matches (caller falls back to legacy verbatim passthrough).
    """
    ref = ref.strip()
    if not ref:
        return None
    exact = load_skill(ref, skills_dir=skills_dir)
    if exact is not None:
        return exact
    needle = ref.lower()
    for skill in discover_skills(skills_dir=skills_dir):
        if skill["name"].lower() == needle:
            return load_skill(skill["name"], skills_dir=skills_dir)
    for skill in discover_skills(skills_dir=skills_dir):
        if _tag_matches(ref, skill):
            return load_skill(skill["name"], skills_dir=skills_dir)
    return None


def render_skill_block(skill: Dict[str, Any], budget_chars: int) -> str:
    """Render one skill as an injectable markdown section, capped at budget."""
    header = f"## Skill: {skill['name']}"
    lines = [header]
    if skill.get("description"):
        lines.append(f"概述: {skill['description']}")
    body = skill.get("body") or ""
    budget = max(200, int(budget_chars))
    if len(body) > budget:
        body = body[:budget].rstrip() + "\n…(技能内容已截断, 详见文件)"
    lines.append(body)
    return "\n".join(lines).strip()


def inject_skills_context(
    parts: List[str],
    load_skills: List[str],
    skills_dir: Optional[Path] = None,
    budget_chars: Optional[int] = None,
    enabled: bool = True,
) -> int:
    """Append real skill content into the worker context ``parts`` list.

    Returns the number of skill files resolved and injected. Unresolvable
    entries are passed through verbatim as legacy one-line instructions
    (migration 008 behaviour), so old ``load_skills`` data keeps working.

    ``enabled=False`` (or env ``SWARM_SKILL_PACKS=0``) disables injection
    entirely — used by ablation baselines.
    """
    if not enabled or not load_skills:
        return 0
    if os.environ.get("SWARM_SKILL_PACKS", "1") == "0":
        return 0
    budget = int(budget_chars or os.environ.get("SWARM_SKILL_BUDGET_CHARS") or DEFAULT_SKILL_BUDGET_CHARS)
    block_budget = max(200, budget // max(1, len(load_skills)))
    block_budget = min(block_budget, int(os.environ.get("SWARM_SKILL_BLOCK_CHARS") or DEFAULT_SKILL_BLOCK_CHARS))

    blocks: List[str] = []
    legacy: List[str] = []
    injected = 0
    for entry in load_skills:
        if not isinstance(entry, str) or not entry.strip():
            continue
        skill = resolve_skill_ref(entry, skills_dir=skills_dir)
        if skill is None:
            legacy.append(entry.strip())
            continue
        blocks.append(render_skill_block(skill, block_budget))
        injected += 1

    if not blocks and not legacy:
        return 0
    section = ["## Role Skills"]
    if blocks:
        section.extend(blocks)
    if legacy:
        section.append("## Role Skill Directives")
        section.extend(f"- {s}" for s in legacy)
    parts.append("\n\n".join(section))
    return injected


def import_skill(source_path: str, name: Optional[str] = None, skills_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Copy an external skill markdown into the swarm skills directory.

    Intended for importing a skill ONCE from another system (e.g. Hermes'
    ~/.hermes/skills/<name>/SKILL.md); after import the swarm owns the file
    and edits live in ``skills/``. Refuses to overwrite an existing skill
    unless ``name`` explicitly re-targets it.
    """
    src = Path(source_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"skill source not found: {src}")
    text = src.read_text(encoding="utf-8", errors="replace")
    meta = parse_frontmatter(text)
    target_name = (name or str(meta.get("name")) or src.stem).strip()
    if not target_name:
        raise ValueError("cannot derive skill name from source; pass --name")
    target_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", target_name).strip("-")

    root = Path(skills_dir) if skills_dir else default_skills_dir()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{target_name}.md"
    if dest.exists():
        raise FileExistsError(f"skill already exists: {dest}")

    # 归一化 frontmatter: 确保 name 与文件名一致, 供 discover/load 使用
    front = ["---", f"name: {target_name}"]
    if meta.get("description"):
        front.append(f"description: {meta['description']}")
    if meta.get("tags"):
        tags = ", ".join(str(t) for t in meta["tags"])
        front.append(f"tags: [{tags}]")
    front.append("---")
    body = FRONTMATTER_RE.sub("", text).strip()
    dest.write_text("\n".join(front) + "\n\n" + body + "\n", encoding="utf-8")
    return {"name": target_name, "path": str(dest), "size_chars": dest.stat().st_size}
