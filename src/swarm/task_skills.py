"""
Task → role → skill/tool 索引表 (migration 010, 2026-08-12).

蜂群收到任务时 (publish_work_task 入队), 按 task_type 查 task_skill_index,
得到执行角色 + 技能引用 + 工具白名单, 写入任务行。静态查表语义:
确定性、可编辑 (UPDATE 表即生效)、最小 (无关键词/语义匹配)。

未命中的 task_type 回退 ROLE_BY_TASK_TYPE (旧行为不变, 技能/工具为空)。

-- 索引表行 → 任务 focus_params:
    focus["task_skills"] = [...load_skills]
    focus["task_tools"]  = [...tool_allowlist]
-- worker 侧: build_task_context 把 task_skills 与角色技能合并 (任务级优先)。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 与 work_queue.ROLE_BY_TASK_TYPE 保持一致的后备 (索引表缺失时使用)。
FALLBACK_ROLE_BY_TASK_TYPE = {
    "scan": "scanner",
    "analyze": "analyst",
    "exploit": "exploiter",
    "report": "reporter",
}


def _json_list(value: Any) -> List[str]:
    import json

    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    return []


def resolve_task_skills(db, task_type: str) -> Optional[Dict[str, Any]]:
    """按 task_type 查索引表; 返回 {role, load_skills, tool_allowlist} 或 None。

    None = 索引表无此行 (调用方回退 ROLE_BY_TASK_TYPE, 技能/工具为空)。
    """
    if not task_type:
        return None
    try:
        row = db.fetch_one(
            "SELECT role, load_skills, tool_allowlist FROM task_skill_index WHERE task_type = ?",
            (task_type,),
        )
    except Exception:  # noqa: BLE001 — 索引表缺失/异常时静默回退
        return None
    if row is None:
        return None
    return {
        "role": str(row["role"] or ""),
        "load_skills": _json_list(row["load_skills"]),
        "tool_allowlist": _json_list(row["tool_allowlist"]),
    }


def index_to_focus(index: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """把索引表结果转成 focus_params 子字段 (task_skills/task_tools)。"""
    if not index:
        return {}
    out: Dict[str, List[str]] = {}
    if index.get("load_skills"):
        out["task_skills"] = index["load_skills"]
    if index.get("tool_allowlist"):
        out["task_tools"] = index["tool_allowlist"]
    return out
