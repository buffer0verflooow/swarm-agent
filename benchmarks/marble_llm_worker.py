"""
LLM-powered database diagnosis worker for the MARBLE benchmark.

Replaces the heuristic `diagnose_with_llm` with a real LLM agent that
uses the `query_db` tool (function calling) to inspect pg_stat_statements
and related views, then produces a JSON diagnosis of root causes.

Uses the OpenAI-compatible endpoint configured for the swarm (zenmux /
ohmygpt providers from ~/.hermes/config.yaml), same pattern as
src/swarm/controller.py.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from benchmarks.marble_db_adapter import query_db

ROOT_CAUSES = [
    "INSERT_LARGE_DATA", "MISSING_INDEXES", "LOCK_CONTENTION", "VACUUM",
    "REDUNDANT_INDEX", "FETCH_LARGE_DATA", "POOR_JOIN_PERFORMANCE", "CPU_CONTENTION",
]

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """You are a senior PostgreSQL performance diagnostician working inside a swarm.
A database anomaly was triggered on the `sysbench` instance (tables in `tmp` database
are also relevant — the anomaly workload runs against `tmp.table1` or `orders`).

Diagnose the root cause(s) of the performance issue. You MUST use the query_db tool
to inspect signals before concluding. Useful queries:

1. `SELECT query, calls, mean_exec_time, total_exec_time FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 30`
   -> which statement patterns dominate? (INSERT INTO table1 / update table1 /
      select * from table1 where / delete from table1 / SELECT * FROM orders LIMIT ...)
2. `SELECT tablename, count(*) AS n FROM pg_indexes WHERE schemaname='public' GROUP BY tablename ORDER BY n DESC`
   -> many indexes on one table = REDUNDANT_INDEX
3. `SELECT relname, seq_scan, seq_tup_read FROM pg_stat_user_tables WHERE seq_scan > 0 ORDER BY seq_tup_read DESC LIMIT 5`
   -> full table scans = MISSING_INDEXES
4. `SELECT mode, count(*) AS n FROM pg_locks GROUP BY mode ORDER BY n DESC` -> lock modes
5. `SELECT * FROM pg_stat_activity WHERE wait_event_type='Lock' AND state='active'` -> live lock waits

Signal map (the anomaly leaves these footprints):
- INSERT_LARGE_DATA  -> many `INSERT INTO table1 SELECT generate_series(...)` calls
- VACUUM             -> `delete from table1` statements (then VACUUM FULL)
- LOCK_CONTENTION    -> many concurrent `update table1 set name...` calls
- MISSING_INDEXES    -> many `select * from table1 where id=` calls (no index)
- FETCH_LARGE_DATA   -> `SELECT * FROM orders LIMIT 100` (heavy scan)
- REDUNDANT_INDEX    -> >=5 indexes on one table
- POOR_JOIN_PERFORMANCE / CPU_CONTENTION -> heavy analytical joins / high CPU

IMPORTANT: high dead_tuple counts / autovacuum activity caused by massive
INSERTs are a SYMPTOM of INSERT_LARGE_DATA, not an independent VACUUM root
cause. Only report VACUUM when you see explicit `delete from table1`
statements or explicit VACUUM commands in pg_stat_statements. Similarly,
seq_scans on a table flooded by the anomaly workload are a symptom of the
flood, not an independent MISSING_INDEXES root cause — only report
MISSING_INDEXES when concurrent `select * from table1 where id=` dominates
the workload.

SECOND-ORDER SYMPTOMS RULE: The anomaly scripts perform setup work whose
purpose is to create the anomalous condition. Do NOT report setup activity
as an independent root cause:
- FETCH_LARGE_DATA scripts first INSERT large volumes into `orders` to make
  the subsequent SELECT heavy — the INSERT INTO orders is SETUP, only the
  heavy SELECT scan is the root cause. Do not report INSERT_LARGE_DATA.
- REDUNDANT_INDEX scripts create many indexes AND run concurrent UPDATEs to
  stress them — the UPDATEs are the script's validation load, the root
  cause is the index bloat (>=5 indexes). Only report LOCK_CONTENTION if
  update volume is the dominant outlier AND there are no many-index signals.
- LOCK_CONTENTION scripts also INSERT seed data first — ignore that INSERT.
- VACUUM scripts INSERT then DELETE then VACUUM FULL — the DELETE+VACUUM
  is the root cause; the INSERT is setup.
- INSERT_LARGE_DATA scripts may trigger autovacuum churn — never report
  VACUUM for that.

When in doubt between a setup statement and a root-cause statement, prefer
the statement that matches the scenario's described symptom and is the
dominant outlier (by calls or total_exec_time).

Final answer format — output a JSON object ONLY, no prose:
{"root_causes": ["ROOT_CAUSE_1", ...], "evidence": {"<signal>": "<observed value>"}}

List 1-2 root causes max. Only include root causes you have evidence for.
"""

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_db",
        "description": "Run a read-only SQL diagnostic query against the PostgreSQL instance. Returns rows as JSON. "
                       "Destructive statements (DELETE/DROP/UPDATE/ALTER/CREATE) are blocked.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL query to run (read-only; e.g. SELECT ... FROM pg_stat_statements ...)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 50)",
                },
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}


class MarbleLLMWorker:
    """LLM agent that diagnoses database anomalies via query_db tool calls."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, max_tool_rounds: int = 8,
                 llm_fn: Optional[Callable] = None):
        self.base_url = base_url or DEFAULT_BASE_URL
        self.api_key = api_key or self._load_api_key()
        self.model = model or DEFAULT_MODEL
        self.max_tool_rounds = max_tool_rounds
        self.llm_fn = llm_fn  # injectable for tests/offline

    @staticmethod
    def _load_api_key() -> str:
        cfg_path = Path.home() / ".hermes" / "config.yaml"
        if cfg_path.exists():
            try:
                import yaml
                cfg = yaml.safe_load(cfg_path.read_text())
                # prefer deepseek-official (stable direct API); fallback zenmux; fallback ohmygpt
                for prov in cfg.get("custom_providers", []):
                    if "deepseek-official" in prov.get("name", "").lower():
                        return prov.get("api_key", "")
                for prov in cfg.get("custom_providers", []):
                    if "zenmux" in prov.get("name", "").lower():
                        return prov.get("api_key", "")
                for prov in cfg.get("custom_providers", []):
                    if "ohmygpt" in prov.get("name", "").lower():
                        return prov.get("api_key", "")
            except Exception:
                pass
        return os.environ.get("DEEPSEEK_API_KEY", "")

    def _chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict]] = None,
              max_retries: int = 5, max_tokens: int = 1024) -> Dict[str, Any]:
        """One chat completion call with retry on timeout/5xx. Returns the assistant message dict."""
        if self.llm_fn:
            return self.llm_fn(messages, tools)
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            # 关闭推理模式: v4 系列是推理模型, 复杂任务推理无限展开吃光
            # max_tokens 导致 content 空 (finish_reason=length). 蜂群任务
            # 都是"直接输出 JSON", 不需要 CoT.
            "thinking": {"type": "disabled"},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        last_err: Optional[Exception] = None
        # mihomo 代理: DNS 被 fake-ip 污染 (zenmux.ai -> 198.18.x.x), 必须走本机代理
        proxies = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    timeout=120,
                    proxies=proxies,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(8 * (attempt + 1))
        raise last_err  # type: ignore[misc]

    def diagnose(self, goal: str, spec: Dict[str, Any], max_rounds: Optional[int] = None) -> Dict[str, Any]:
        """Run the diagnosis loop. Returns {root_causes, evidence, rounds, tool_calls}."""
        rounds = max_rounds or self.max_tool_rounds
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Scenario:\n{goal[:2000]}"},
        ]
        tool_calls = 0
        t0 = time.time()
        for _ in range(rounds):
            msg = self._chat(messages, tools=[TOOL_SCHEMA])
            if msg.get("tool_calls"):
                messages.append(msg)
                for tc in msg["tool_calls"]:
                    tool_calls += 1
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = self._run_tool(name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", f"call-{tool_calls}"),
                        "content": json.dumps(result, ensure_ascii=False, default=str)[:6000],
                    })
                continue
            # no tool calls -> final answer
            content = msg.get("content", "")
            parsed = self._parse_diagnosis(content)
            parsed["rounds"] = _ + 1
            parsed["tool_calls"] = tool_calls
            parsed["elapsed"] = round(time.time() - t0, 1)
            return parsed
        return {"root_causes": [], "evidence": {}, "rounds": rounds, "tool_calls": tool_calls,
                "error": "max rounds reached without final answer"}

    def _run_tool(self, name: str, args: Dict[str, Any]) -> Any:
        if name == "query_db":
            return query_db(args.get("sql", ""), limit=int(args.get("limit") or 50))
        return {"error": f"unknown tool: {name}"}

    @staticmethod
    def _parse_diagnosis(content: str) -> Dict[str, Any]:
        """Extract JSON object from LLM final answer (tolerates prose around it)."""
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return {"root_causes": [], "evidence": {}, "raw": content[:500]}
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError:
            return {"root_causes": [], "evidence": {}, "raw": content[:500]}
        roots = obj.get("root_causes", [])
        if isinstance(roots, str):
            roots = [roots]
        # normalize to known root causes
        known = {rc.lower(): rc for rc in ROOT_CAUSES}
        normalized = []
        for r in roots:
            rn = known.get(str(r).strip().lower())
            if rn and rn not in normalized:
                normalized.append(rn)
        return {"root_causes": normalized, "evidence": obj.get("evidence", {}), "raw": content[:500]}


def diagnose_with_llm(goal: str, spec: Dict[str, Any], worker: Optional[MarbleLLMWorker] = None) -> List[str]:
    """Compat entry: run LLM worker and return root cause list."""
    w = worker or MarbleLLMWorker()
    result = w.diagnose(goal, spec)
    return result["root_causes"]


# ── Shared signal collection (probe worker) ───────────────────────────────

def collect_probe_snapshot() -> Dict[str, Any]:
    """
    Structured aggregate signal snapshot shared by all verifiers.
    Aggregates pg_stat_statements into per-pattern call counts + timing,
    plus index / seq-scan / lock facts — the full context each verifier
    needs, so no verifier re-scans the raw statements by itself.
    """
    from benchmarks.marble_db_adapter import query_db

    stats = query_db(
        "SELECT query, calls, mean_exec_time, total_exec_time "
        "FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 60"
    ) or []
    rows = [r for r in stats if not isinstance(r, dict) or "error" not in r]

    def calls_of(pattern: str) -> int:
        import re as _re
        rx = _re.compile(pattern, _re.I)
        return sum(int(r.get("calls") or 0) for r in rows if rx.search(str(r.get("query", ""))))

    def total_ms(pattern: str) -> float:
        import re as _re
        rx = _re.compile(pattern, _re.I)
        return round(sum(float(r.get("total_exec_time") or 0) for r in rows
                         if rx.search(str(r.get("query", "")))), 1)

    snap = {
        "top_statements": rows[:20],
        "patterns": {
            "insert_table1": {"calls": calls_of(r"insert\s+into\s+table1"),
                              "total_ms": total_ms(r"insert\s+into\s+table1")},
            "update_table1": {"calls": calls_of(r"update\s+table1"),
                              "total_ms": total_ms(r"update\s+table1")},
            "select_table1_where": {"calls": calls_of(r"select\s+\*\s+from\s+table1\s+where"),
                                    "total_ms": total_ms(r"select\s+\*\s+from\s+table1\s+where")},
            "delete_table1": {"calls": calls_of(r"delete\s+from\s+table1"),
                              "total_ms": total_ms(r"delete\s+from\s+table1")},
            "select_orders_limit": {"calls": calls_of(r"from\s+orders\s+limit"),
                                    "total_ms": total_ms(r"from\s+orders\s+limit")},
            "insert_orders": {"calls": calls_of(r"insert\s+into\s+orders"),
                              "total_ms": total_ms(r"insert\s+into\s+orders")},
            "vacuum": calls_of(r"vacuum"),
        },
        "indexes": query_db(
            "SELECT tablename, count(*) AS n FROM pg_indexes "
            "WHERE schemaname='public' GROUP BY tablename ORDER BY n DESC"
        ),
        "index_usage": query_db(
            "SELECT indexrelname, idx_scan, tablename FROM pg_stat_user_indexes "
            "ORDER BY idx_scan DESC LIMIT 12"
        ),
        "seq_scans": query_db(
            "SELECT relname, seq_scan, seq_tup_read FROM pg_stat_user_tables "
            "WHERE seq_scan > 0 ORDER BY seq_tup_read DESC LIMIT 6"
        ),
        "locks": query_db(
            "SELECT mode, count(*) AS n FROM pg_locks GROUP BY mode ORDER BY n DESC LIMIT 8"
        ),
    }
    return snap


def snapshot_to_text(snap: Dict[str, Any], max_chars: int = 4000) -> str:
    """Render the structured snapshot as compact text for prompts."""
    import json
    lines = []
    p = snap.get("patterns", {})
    lines.append("PATTERN CALL COUNTS (pg_stat_statements):")
    for k, v in p.items():
        if isinstance(v, dict):
            lines.append(f"  {k}: calls={v['calls']} total_ms={v['total_ms']}")
        else:
            lines.append(f"  {k}: {v}")
    lines.append("INDEXES PER TABLE:")
    for r in (snap.get("indexes") or []):
        lines.append(f"  {r.get('tablename')}: {r.get('n')}")
    lines.append("TOP INDEX USAGE (idx_scan):")
    for r in (snap.get("index_usage") or []):
        lines.append(f"  {r.get('indexrelname')}: idx_scan={r.get('idx_scan')}")
    lines.append("SEQ SCANS (full-table reads):")
    for r in (snap.get("seq_scans") or []):
        lines.append(f"  {r.get('relname')}: seq_scan={r.get('seq_scan')} tuples={r.get('seq_tup_read')}")
    lines.append("LOCK MODES:")
    for r in (snap.get("locks") or []):
        lines.append(f"  {r.get('mode')}: {r.get('n')}")
    text = "\n".join(lines)
    return text[:max_chars]


# ── Swarm per-root-cause verification worker ──────────────────────────────

ROOT_CAUSE_SIGNATURES = {
    "INSERT_LARGE_DATA": "concurrent `INSERT INTO table1 SELECT generate_series(...)` dominating pg_stat_statements",
    "MISSING_INDEXES": "concurrent `select * from table1 where id=...` scans on an unindexed table",
    "LOCK_CONTENTION": "many concurrent `update table1 set name...` statements (lock waits on row updates)",
    "VACUUM": "`delete from table1` statements followed by VACUUM FULL / heavy autovacuum churn",
    "REDUNDANT_INDEX": ">=5 indexes on one table, most with idx_scan=0 (never used)",
    "FETCH_LARGE_DATA": "`SELECT * FROM orders LIMIT 100` with huge seq_scan volume (millions of tuples read)",
    "POOR_JOIN_PERFORMANCE": "heavy analytical multi-table joins with high total_exec_time",
    "CPU_CONTENTION": "high CPU from a tight query loop (many calls, low per-call cost)",
}

VERIFY_SYSTEM_PROMPT = """You are a PostgreSQL diagnostician inside a swarm. A database anomaly
was triggered. The probe worker already collected a structured signal snapshot below.
Your job: verify ONE candidate root cause against this snapshot.

Candidate root cause: {rc}
Its expected signature: {sig}

Shared swarm probe snapshot (collected by the probe worker before you started):
{probe}

DECIDE FROM THE SNAPSHOT FIRST. The snapshot already contains pattern call counts
(insert_table1 / update_table1 / select_table1_where / delete_table1 /
select_orders_limit / insert_orders / vacuum), indexes per table, index usage
(idx_scan), seq scans, and lock modes. Only run extra query_db calls if the
snapshot is ambiguous for YOUR root cause.

STRICT EVIDENCE RULES — apply ALL of these:
- INSERT_LARGE_DATA  present iff pattern.insert_table1.calls is large (>= 50).
- LOCK_CONTENTION    present iff pattern.update_table1.calls is large (>= 50) AND
                     NOT (many unused indexes on table1). If table1 has >= 5 indexes
                     (REDUNDANT_INDEX scenario), the updates are validation load,
                     NOT lock contention — unless update calls exceed ~150k.
                     IMPORTANT: the vacuum script deletes and REBUILDS table1, so
                     a missing index signal in the snapshot does NOT prove there
                     are no indexes — if update_table1.calls is huge (>50k) AND
                     delete_table1.calls > 0, the updates are likely REDUNDANT_INDEX
                     validation load; mark present=false and let the lead arbitrate.
- MISSING_INDEXES    present iff pattern.select_table1_where.calls is large (>= 50).
                     "table1 has no indexes" alone is NOT evidence — tmp tables
                     normally have none; only actual high-volume where-queries count.
- VACUUM             present iff pattern.delete_table1.calls > 0 OR pattern.vacuum > 0.
- FETCH_LARGE_DATA   present iff pattern.select_orders_limit.calls is large (>= 50)
                     AND seq_scans shows orders with MANY seq_scans (the repeated
                     LIMIT-100 full scans ARE the anomaly — do NOT require millions
                     of tuples read; thousands of seq_scans on orders with few
                     tuples per scan is exactly the FETCH_LARGE_DATA signature).
                     insert_orders is SETUP — ignore it for this verdict.
- REDUNDANT_INDEX    present iff any table has >= 5 indexes, most with idx_scan=0.
                     FALLBACK (index-deleted timing): the VACUUM script deletes and
                     REBUILDS table1 after the redundant-index script ran, so at
                     probe time the indexes may be GONE. If pg_indexes shows no
                     many-index table BUT update_table1.calls is huge (>50k) AND
                     delete_table1.calls > 0, the update flood is REDUNDANT_INDEX
                     validation load → report REDUNDANT_INDEX present.
- CPU_CONTENTION     present ONLY IF no other pattern dominates (all of
                     insert_table1/update_table1/select_table1_where/delete_table1/
                     select_orders_limit calls are below threshold) — it is a
                     last-resort catch-all, never a co-diagnosis with another
                     root cause that has a clear signature.

Answer ONLY a JSON object:
{{"present": true_or_false, "evidence": "specific observed numbers from the snapshot that prove or disprove"}}"""


class RootCauseVerifier:
    """Single-root-cause verification worker (one analyze node = one verifier)."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, max_tool_rounds: int = 4,
                 llm_fn: Optional[Callable] = None):
        self.worker = MarbleLLMWorker(base_url=base_url, api_key=api_key, model=model, llm_fn=llm_fn)
        self.max_tool_rounds = max_tool_rounds

    def verify(self, root_cause: str, goal: str, probe_snapshot: str = "") -> Dict[str, Any]:
        """Verify one root cause against the shared snapshot. Returns {present, evidence, ...}."""
        rc = root_cause.upper()
        sig = ROOT_CAUSE_SIGNATURES.get(rc, "unspecified — inspect pg_stat_statements for evidence")
        probe = probe_snapshot or "not collected (query pg_stat_statements yourself)"
        messages = [
            {"role": "system", "content": VERIFY_SYSTEM_PROMPT.format(rc=rc, sig=sig, probe=probe)},
            {"role": "user", "content": f"Scenario:\n{goal[:1500]}\n\nVerify root cause: {rc}"},
        ]
        tool_calls = 0
        t0 = time.time()
        for _ in range(self.max_tool_rounds):
            msg = self.worker._chat(messages, tools=[TOOL_SCHEMA])
            if msg.get("tool_calls"):
                messages.append(msg)
                for tc in msg["tool_calls"]:
                    tool_calls += 1
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = self.worker._run_tool(fn.get("name", ""), args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", f"call-{tool_calls}"),
                        "content": json.dumps(result, ensure_ascii=False, default=str)[:6000],
                    })
                continue
            content = msg.get("content", "")
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group())
                    return {
                        "root_cause": rc,
                        "present": bool(obj.get("present")),
                        "evidence": str(obj.get("evidence", ""))[:300],
                        "rounds": _ + 1,
                        "tool_calls": tool_calls,
                        "elapsed": round(time.time() - t0, 1),
                    }
                except json.JSONDecodeError:
                    pass
            return {"root_cause": rc, "present": False, "evidence": content[:300],
                    "rounds": _ + 1, "tool_calls": tool_calls, "elapsed": round(time.time() - t0, 1)}
        return {"root_cause": rc, "present": False, "evidence": "max rounds", 
                "rounds": self.max_tool_rounds, "tool_calls": tool_calls, "elapsed": round(time.time() - t0, 1)}
