"""
集成测试：3 个 mock Agent 跑一个完整 swarm run。

验证:
  1. Agent 注册 + 心跳 + 超时清理
  2. Spawn 请求写入 + 轮询 + 履行
  3. Capture 自动触发 spawn
  4. Orchestrator 主循环
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import SwarmDB
from src.agents.capture import CaptureContext, CaptureSource, capture
from src.agents.extractor import (
    ExtractedKnowledge,
    extract_knowledge_from_text,
    format_extraction_for_insert,
    generate_insert_sql,
    generate_lineage_sql,
)
from src.swarm.lifecycle import AgentLifecycle, cleanup_stale_agents, get_live_agents
from src.swarm.spawner import (
    request_spawn, poll_spawn_requests, claim_spawn_requests,
    mark_spawn_fulfilled, mark_spawn_rejected,
    merge_duplicate_requests, expire_old_requests, recover_stale_spawn_claims,
)
from src.swarm.work_queue import (
    publish_work_task, poll_work_tasks, claim_work_tasks,
    recover_stale_work_claims, complete_work_task,
)
from src.swarm.model_config import (
    build_run_summary,
    get_model_profile,
    list_model_profiles,
    record_swarm_event,
    upsert_model_profile,
)
from src.swarm.artifacts import verify_artifacts
from src.swarm.client_api import (
    get_swarm_result,
    get_swarm_status,
    submit_swarm_task,
)
from src.swarm.worker import SwarmWorker, build_task_context
from src.swarm.run_manager import create_seeded_swarm_run
from src.swarm.runner import SwarmRunner, adapt_executor_factory
from src.swarm.orchestrator import SwarmOrchestrator, POLL_SPAWN_SEC, POLL_HEARTBEAT_SEC
from src.swarm.spawn_handler import HermesSpawnHandler
from src.governance.engine import check_and_decay
from src.governance.verification import auto_enqueue_validations, process_validation_queue
from src.governance.bounty import (
    create_finding_hypothesis,
    get_negative_knowledge,
    rank_hypotheses_by_roi,
    record_gate_result,
)
from src.ontology.discovery import discover_relations_from_cooccurrence
from src.ontology.inference import infer_transitive_relations


# ── Setup ──

def setup_test_db():
    """创建临时测试数据库"""
    db_path = "/tmp/test_swarm_loop.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    db = SwarmDB(db_path)
    db.init()
    return db


def create_test_run(db) -> str:
    """创建测试 swarm_run"""
    run_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO swarm_runs (run_id, swarm_name, intent, target_type, target_id, status)
           VALUES (?, 'test-swarm', 'recon', 'webapp', 'test-target', 'running')""",
        (run_id,),
    )
    db.conn.commit()
    return run_id


# ── Tests ──

def test_lifecycle():
    """测试 Agent 注册 → 心跳 → 清理"""
    print("\n=== Test: Lifecycle ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    # 注册 3 个 Agent
    agents = []
    for role in ("scanner", "analyst", "exploiter"):
        agent_id = f"test-{role}-001"
        lc = AgentLifecycle(db, agent_id, run_id)
        lc.register(role=role, capabilities=[f"mock_{role}"])
        agents.append((agent_id, lc))
        print(f"  registered: {agent_id}")

    # 验证 agent_profiles
    for agent_id, _ in agents:
        row = db.fetch_one("SELECT role, status FROM agent_profiles WHERE agent_id=?", (agent_id,))
        assert row["status"] == "active", f"{agent_id}: expected active, got {row['status']}"
    print("  ✅ agent_profiles populated")

    # 验证心跳表
    live = get_live_agents(db, run_id)
    assert len(live) == 3, f"expected 3 live agents, got {len(live)}"
    print(f"  ✅ {len(live)} live agents detected")

    # 创建 dummy task 满足 FK 约束
    for agent_id, _ in agents:
        task_id = f"task-{agent_id}"
        db.execute(
            """INSERT INTO agent_tasks (task_id, run_id, agent_id, task_type, status)
               VALUES (?, ?, ?, 'subtask', 'running')""",
            (task_id, run_id, agent_id),
        )
    db.conn.commit()

    # 发送心跳
    for agent_id, lc in agents:
        lc.beat(current_task_id=f"task-{agent_id}", load=0.3)
    print("  ✅ heartbeats sent")

    # 注销一个 Agent
    agents[0][1].deregister()
    print("  ✅ agent deregistered")

    # 验证减少
    live_after = get_live_agents(db, run_id)
    assert len(live_after) == 2, f"expected 2 live agents, got {len(live_after)}"
    print("  ✅ deregistration reflected in heartbeat table")

    # 验证 profile 状态变更
    row = db.fetch_one("SELECT status FROM agent_profiles WHERE agent_id=?", (agents[0][0],))
    assert row["status"] == "idle", f"expected idle, got {row['status']}"
    print("  ✅ profile status updated to idle")

    db.close()
    print("=== Lifecycle: ALL PASSED ===")


def test_spawn():
    """测试 spawn 请求写入 → 轮询 → 履行"""
    print("\n=== Test: Spawn ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    # 注册 requesting agent
    lc = AgentLifecycle(db, "test-scanner-001", run_id)
    lc.register(role="scanner")

    # 写入 spawn 请求
    req_id = request_spawn(
        db, run_id,
        requesting_agent="test-scanner-001",
        requested_role="exploiter",
        reason="Found SQL injection vulnerability",
        context_entry_ids=["entry-001", "entry-002"],
        parent_task_id=None,
    )
    print(f"  spawn request created: {req_id[:8]}")

    # 轮询 pending 请求
    pending = poll_spawn_requests(db, run_id)
    assert len(pending) == 1, f"expected 1 pending, got {len(pending)}"
    req = pending[0]
    assert req["requested_role"] == "exploiter"
    assert req["status"] == "pending"
    print(f"  ✅ poll returned 1 pending request: {req['requested_role']}")

    # 履行 — 需要先注册目标 agent 满足 FK 约束
    lc2 = AgentLifecycle(db, "test-exploiter-001", run_id)
    lc2.register(role="exploiter")
    mark_spawn_fulfilled(db, req_id, "test-exploiter-001")
    req_after = db.fetch_one("SELECT status, spawned_agent_id FROM spawn_requests WHERE request_id=?", (req_id,))
    assert req_after["status"] == "fulfilled"
    assert req_after["spawned_agent_id"] == "test-exploiter-001"
    print("  ✅ spawn fulfilled")

    # 重复请求合并: 新请求用 dedup_key 直接复用 pending 请求
    dupe_reason = "same reporter duplicate"
    first_dupe = request_spawn(db, run_id, "test-scanner-001", "reporter", dupe_reason, priority=50)
    second_dupe = request_spawn(db, run_id, "test-scanner-001", "reporter", dupe_reason, priority=51)
    assert first_dupe == second_dupe

    # 模拟旧库里缺少 dedup_key 的重复 pending 行，验证 merge_duplicate_requests 会按新 key 合并
    for i in range(2):
        db.execute(
            """INSERT INTO spawn_requests
               (request_id, run_id, requesting_agent, requested_role, reason, context_entry_ids, priority)
               VALUES (?, ?, 'test-scanner-001', 'reporter', ?, '[]', ?)""",
            (str(uuid.uuid4()), run_id, dupe_reason, 52 + i),
        )
    db.conn.commit()
    merged = merge_duplicate_requests(db, run_id)
    assert merged == 2, f"expected 2 duplicate merges, got {merged}"
    print(f"  ✅ merged {merged} duplicate reporter requests")

    # 原子 claim: 第一次领取成功，第二次不能重复领取同一条请求
    claim_req = request_spawn(
        db, run_id, "test-scanner-001", "analyst",
        "Need independent analysis", ttl_minutes=1, priority=70,
    )
    claimed = claim_spawn_requests(db, run_id, limit=1)
    assert len(claimed) == 1 and claimed[0]["request_id"] == claim_req
    assert claimed[0]["status"] == "spawning"
    claimed_row = db.fetch_one("SELECT status, claimed_at FROM spawn_requests WHERE request_id=?", (claim_req,))
    assert claimed_row["status"] == "spawning" and claimed_row["claimed_at"]
    claimed_again = claim_spawn_requests(db, run_id, limit=5)
    assert all(r["request_id"] != claim_req for r in claimed_again)

    db.execute("UPDATE spawn_requests SET claimed_at = datetime('now', '-5 minutes') WHERE request_id=?", (claim_req,))
    db.conn.commit()
    recovered = recover_stale_spawn_claims(db, stale_seconds=60)
    assert recovered == 1
    recovered_claim = claim_spawn_requests(db, run_id, limit=1)
    assert recovered_claim and recovered_claim[0]["request_id"] == claim_req

    mark_spawn_rejected(db, claim_req, "test_cleanup")
    for req in claimed_again:
        mark_spawn_rejected(db, req["request_id"], "test_cleanup")
    print("  ✅ spawn claim is single-consumer")

    # 过期清理
    # 手动设置一个过期
    expire_req = request_spawn(db, run_id, "test-scanner-001", "reporter", "expire me")
    db.execute(
        "UPDATE spawn_requests SET expires_at = datetime('now', '-1 minute') "
        "WHERE request_id = ?",
        (expire_req,),
    )
    expired = expire_old_requests(db)
    assert expired == 1, f"expected 1 expired request, got {expired}"
    print(f"  ✅ expired {expired} old requests")

    db.close()
    print("=== Spawn: ALL PASSED ===")


def test_work_market_claims_are_atomic():
    """测试共享任务市场: 发布、去重、按角色原子领取、卡住 claim 恢复"""
    print("\n=== Test: Work Market Claims ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    for agent_id in ("analyst-a", "analyst-b"):
        AgentLifecycle(db, agent_id, run_id).register(role="analyst")

    task_1 = publish_work_task(
        db, run_id, "analyze", "analyst",
        "Analyze finding A", context_entry_ids=["entry-a"],
        source_agent="scanner-a", priority=80,
    )
    task_1_dupe = publish_work_task(
        db, run_id, "analyze", "analyst",
        "Duplicate analyze finding A", context_entry_ids=["entry-a"],
        source_agent="scanner-b", priority=60,
    )
    task_2 = publish_work_task(
        db, run_id, "analyze", "analyst",
        "Analyze finding B", context_entry_ids=["entry-b"],
        source_agent="scanner-a", priority=70,
    )

    assert task_1 == task_1_dupe, "same role/type/context should dedupe active work"
    pending = poll_work_tasks(db, run_id=run_id, required_role="analyst")
    assert len(pending) == 2

    claimed_a = claim_work_tasks(db, run_id, "analyst-a", "analyst", limit=1)
    claimed_b = claim_work_tasks(db, run_id, "analyst-b", "analyst", limit=1)
    assert claimed_a and claimed_b
    assert claimed_a[0]["task_id"] != claimed_b[0]["task_id"]
    assert {claimed_a[0]["task_id"], claimed_b[0]["task_id"]} == {task_1, task_2}

    claimed_again = claim_work_tasks(db, run_id, "analyst-a", "analyst", limit=1)
    assert claimed_again == []

    db.execute("UPDATE agent_tasks SET claimed_at = datetime('now', '-30 minutes') WHERE task_id = ?", (task_1,))
    db.conn.commit()
    recovered = recover_stale_work_claims(db, stale_seconds=60)
    assert recovered == 1
    recovered_claim = claim_work_tasks(db, run_id, "analyst-a", "analyst", limit=1)
    assert recovered_claim and recovered_claim[0]["task_id"] == task_1
    complete_work_task(db, task_1, {"summary": "done"})

    db.close()
    print("  ✅ work market supports dedupe and single-consumer claims")


def test_work_market_generations_follow_parent_tasks():
    """测试 follow-up task 继承 parent iteration + 1"""
    print("\n=== Test: Work Market Generation ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    parent_id = publish_work_task(
        db,
        run_id,
        "scan",
        "scanner",
        "Seed generation",
        source_agent="system",
        generation=2,
    )
    child_id = publish_work_task(
        db,
        run_id,
        "analyze",
        "analyst",
        "Follow parent generation",
        parent_task_id=parent_id,
        context_entry_ids=["entry-generation"],
        source_agent="scanner-001",
    )
    parent = db.fetch_one("SELECT iteration FROM agent_tasks WHERE task_id=?", (parent_id,))
    child = db.fetch_one("SELECT iteration, focus_params FROM agent_tasks WHERE task_id=?", (child_id,))
    assert parent["iteration"] == 2
    assert child["iteration"] == 3
    assert json.loads(child["focus_params"])["generation"] == 3

    db.close()
    print("  ✅ follow-up work increments generation")


def test_model_profiles_are_swarm_owned():
    """测试模型配置由蜂群维护，并随任务领取返回给外部调用端"""
    print("\n=== Test: Swarm-owned Model Profiles ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    defaults = list_model_profiles(db, enabled_only=True)
    roles = {p["role"] for p in defaults if p["is_default"]}
    assert {"scanner", "analyst", "exploiter", "reporter", "custom"}.issubset(roles)

    profile_id = upsert_model_profile(
        db,
        role="analyst",
        provider="claude",
        model="sonnet-test",
        priority=95,
        is_default=True,
        max_tokens=24000,
        temperature=0.1,
        tool_policy={"shell": True, "network": False},
        metadata={"configured_by": "test"},
    )
    selected = get_model_profile(db, "analyst")
    assert selected["profile_id"] == profile_id
    assert selected["provider"] == "claude"
    assert selected["model"] == "sonnet-test"

    task_id = publish_work_task(
        db,
        run_id,
        "analyze",
        "analyst",
        "Analyze model profile handoff",
        context_entry_ids=[],
        source_agent="test",
        priority=90,
    )
    row = db.fetch_one("SELECT model_profile_id FROM agent_tasks WHERE task_id=?", (task_id,))
    assert row["model_profile_id"] == profile_id

    worker = SwarmWorker(db, run_id, "analyst-profile-001", "analyst")
    claimed = worker.claim_once()
    assert claimed["task"]["task_id"] == task_id
    assert claimed["model_profile"]["profile_id"] == profile_id
    assert claimed["model_profile"]["provider"] == "claude"

    agent = db.fetch_one(
        "SELECT model_profile_id, model_preference FROM agent_profiles WHERE agent_id=?",
        ("analyst-profile-001",),
    )
    assert agent["model_profile_id"] == profile_id
    assert agent["model_preference"] == "claude:sonnet-test"

    record_swarm_event(
        db,
        run_id,
        event_type="client_message",
        source="codex",
        agent_id="analyst-profile-001",
        task_id=task_id,
        content="External client accepted swarm-selected model profile.",
    )
    complete_work_task(db, task_id, {"summary": "done", "model_profile_id": profile_id})
    summary = build_run_summary(db, run_id)
    assert any(m["profile_id"] == profile_id for m in summary["models"])
    assert any(e["source"] == "codex" for e in summary["events"])
    stored = db.fetch_one("SELECT conversation_summary FROM swarm_runs WHERE run_id=?", (run_id,))
    assert "Models:" in stored["conversation_summary"]

    db.close()
    print("  ✅ model profiles are selected by swarm and exposed to clients")


def test_seeded_swarm_run_publishes_market_tasks():
    """测试 run 初始化入口发布并行市场任务，而不是四阶段 parent 链"""
    print("\n=== Test: Seeded Swarm Run ===")
    db = setup_test_db()
    result = create_seeded_swarm_run(
        db,
        swarm_name="hackone-swarm",
        intent="recon",
        target_type="webapp",
        target_id="example.test",
        profile="breadth",
    )
    run_id = result["run_id"]
    assert run_id
    assert len(result["seeded_tasks"]) >= 5
    assert all(t.get("model_profile") for t in result["seeded_tasks"])
    assert result["min_agents_by_role"]["scanner"] >= 4
    assert result["max_agents"] == 8

    tasks = db.fetch_all(
        """SELECT task_id, task_type, required_role, parent_task_id, agent_id,
                  status, focus_params, signal_key
           FROM agent_tasks WHERE run_id = ? ORDER BY priority DESC""",
        (run_id,),
    )
    roles = {t["required_role"] for t in tasks}
    assert {"scanner", "analyst", "reporter"}.issubset(roles)
    assert all(t["status"] == "pending" for t in tasks)
    assert all(t["agent_id"] is None for t in tasks)
    assert all(t["parent_task_id"] is None for t in tasks)
    assert all((t["signal_key"] or "").startswith("seed:") for t in tasks)
    assert all("phase" not in json.loads(t["focus_params"]) for t in tasks)
    run_config = json.loads(db.fetch_one("SELECT config FROM swarm_runs WHERE run_id=?", (run_id,))["config"])
    assert run_config["min_agents_by_role"]["scanner"] >= 4
    assert run_config["generation"] == 1

    duplicate = create_seeded_swarm_run(
        db,
        swarm_name="hackone-swarm-2",
        intent="recon",
        target_type="webapp",
        target_id="example-2.test",
        profile="balanced",
    )
    assert duplicate["run_id"] != run_id

    db.close()
    print("  ✅ run manager seeds independent market tasks")


async def test_swarm_runner_executes_multi_worker_pool():
    """测试自动 runner 按 min_agents_by_role 启动 worker 池并消费任务市场"""
    print("\n=== Test: Swarm Runner ===")
    db = setup_test_db()
    result = create_seeded_swarm_run(
        db,
        swarm_name="runner-swarm",
        intent="analyze",
        target_type="webapp",
        target_id="runner.example.test",
        profile="balanced",
        config={"min_agents_by_role": {"analyst": 2, "reporter": 1}, "max_agents": 3},
    )
    run_id = result["run_id"]
    total_tasks = db.fetch_one("SELECT COUNT(*) AS c FROM agent_tasks WHERE run_id=?", (run_id,))["c"]
    assert total_tasks >= 3

    def executor_factory(role, agent_id):
        def executor(task, context):
            assert "Task" in context
            return {
                "capture": False,
                "content": f"{agent_id} completed {task['task_type']}",
                "token_cost": 1,
            }
        return executor

    runner = SwarmRunner(db)
    run_result = await runner.run_until_idle(
        run_id,
        adapt_executor_factory(executor_factory),
        max_rounds=10,
        idle_round_limit=1,
    )

    assert run_result.workers == 3
    assert run_result.processed == total_tasks
    assert run_result.task_counts.get("completed") == total_tasks
    live = get_live_agents(db, run_id)
    assert len(live) == 3

    db.close()
    print("  ✅ runner starts multiple workers and completes market tasks")


def test_start_swarm_cli_outputs_seeded_run():
    """测试 swarmctl.py run CLI 创建 run 并输出 seed tasks（原 start_swarm.py 吸收后）"""
    print("\n=== Test: Start Swarm CLI ===\n")
    db_path = "/tmp/test_start_swarm_cli.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "swarmctl.py"),
            "--db", db_path,
            "run",
            "--name", "cli-swarm",
            "--intent", "recon",
            "--target-type", "webapp",
            "--target", "cli.example.test",
            "--profile", "balanced",
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run_id"]
    assert len(payload["seeded_tasks"]) >= 5
    assert all(t.get("model_profile") for t in payload["seeded_tasks"])

    db = SwarmDB(db_path)
    run = db.fetch_one("SELECT intent, target_id FROM swarm_runs WHERE run_id=?", (payload["run_id"],))
    assert run["intent"] == "recon"
    assert run["target_id"] == "cli.example.test"
    pending = poll_work_tasks(db, run_id=payload["run_id"], status="pending", limit=20)
    assert len(pending) == len(payload["seeded_tasks"])
    assert {p["required_role"] for p in pending}.issuperset({"scanner", "analyst", "reporter"})
    assert all(p["model_profile_id"] for p in pending)
    db.close()
    print("  ✅ start_swarm.py creates market-driven runs")


def test_client_api_submits_task_and_fetches_result():
    """测试外部客户端下发高层任务，再从蜂群获取结果"""
    print("\n=== Test: Client API Task Submit/Result ===")
    db = setup_test_db()
    submitted = submit_swarm_task(
        db,
        task="Map example.test and produce a concise authorized recon result.",
        client_source="hermes",
        intent="recon",
        target_type="webapp",
        target_id="example.test",
        profile="balanced",
        metadata={"ticket": "H1-test"},
    )
    run_id = submitted["run_id"]
    assert run_id
    assert submitted["seeded_tasks"]

    event = db.fetch_one(
        "SELECT event_type, source, content FROM swarm_conversation_events WHERE event_id=?",
        (submitted["event_id"],),
    )
    assert event["event_type"] == "client_task_submitted"
    assert event["source"] == "hermes"
    assert "example.test" in event["content"]

    status = get_swarm_status(db, run_id)
    assert status["status"] == "running"
    assert status["ready"] is False
    assert status["config"]["client_source"] == "hermes"

    task = db.fetch_one("SELECT task_id, focus_params FROM agent_tasks WHERE run_id=? LIMIT 1", (run_id,))
    assert "client_objective" in json.loads(task["focus_params"])

    tasks = db.fetch_all("SELECT task_id, task_type FROM agent_tasks WHERE run_id=?", (run_id,))
    report_task = next((t for t in tasks if t["task_type"] == "report"), tasks[0])
    db.execute("UPDATE agent_tasks SET status='running' WHERE task_id=?", (report_task["task_id"],))
    db.conn.commit()
    complete_work_task(
        db,
        report_task["task_id"],
        {"content": "Final swarm result: example.test has recon evidence and a concise report."},
    )
    for t in tasks:
        if t["task_id"] == report_task["task_id"]:
            continue
        db.execute(
            """UPDATE agent_tasks
               SET status='completed',
                   result_summary='{"content":"internal task completed"}',
                   ended_at=datetime('now'),
                   updated_at=datetime('now')
               WHERE task_id=?""",
            (t["task_id"],),
        )
    db.conn.commit()

    result = get_swarm_result(db, run_id)
    assert result["ready"] is True
    assert result["status"] == "completed"
    assert "Final swarm result" in result["result"]

    db.close()
    print("  ✅ external clients submit tasks and fetch swarm results")


def test_swarmctl_cli_task_submit_status_result():
    """测试 swarmctl task CLI 是外部下发任务/取结果入口"""
    print("\n=== Test: Swarm Control Task CLI ===")
    db_path = "/tmp/test_swarmctl_task_cli.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    submit = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "swarmctl.py"),
            "--db", db_path,
            "task", "submit",
            "--source", "claude",
            "--task", "Analyze cli.example.test and return a concise swarm result.",
            "--intent", "analyze",
            "--target-type", "webapp",
            "--target", "cli.example.test",
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert submit.returncode == 0, submit.stderr
    payload = json.loads(submit.stdout)
    run_id = payload["run_id"]
    assert payload["seeded_tasks"]

    status = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "swarmctl.py"),
            "--db", db_path,
            "task", "status",
            "--run-id", run_id,
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["status"] == "running"
    assert status_payload["config"]["client_source"] == "claude"

    db = SwarmDB(db_path)
    tasks = db.fetch_all("SELECT task_id, task_type FROM agent_tasks WHERE run_id=?", (run_id,))
    for task in tasks:
        content = "CLI final swarm result" if task["task_type"] == "report" else "internal completed"
        db.execute(
            """UPDATE agent_tasks
               SET status='completed',
                   result_summary=?,
                   ended_at=datetime('now'),
                   updated_at=datetime('now')
               WHERE task_id=?""",
            (json.dumps({"content": content}), task["task_id"]),
        )
    db.conn.commit()
    db.close()

    result = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "swarmctl.py"),
            "--db", db_path,
            "task", "result",
            "--run-id", run_id,
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    result_payload = json.loads(result.stdout)
    assert result_payload["ready"] is True
    assert result_payload["status"] == "completed"
    assert "CLI final swarm result" in result_payload["result"]

    print("  ✅ swarmctl.py task submit/status/result works as the client boundary")


def test_swarmctl_cli_models_event_summary():
    """测试 swarmctl.py 维护模型配置、记录事件、输出 summary"""
    print("\n=== Test: Swarm Control CLI ===")
    db_path = "/tmp/test_swarmctl_cli.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    db = SwarmDB(db_path)
    db.init()
    run_id = create_test_run(db)
    db.close()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    set_result = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "swarmctl.py"),
            "--db", db_path,
            "models", "set",
            "--role", "scanner",
            "--provider", "codex",
            "--model", "gpt-test",
            "--default",
            "--metadata", '{"configured_by":"cli-test"}',
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert set_result.returncode == 0, set_result.stderr
    set_payload = json.loads(set_result.stdout)
    assert set_payload["profile"]["provider"] == "codex"
    assert set_payload["profile"]["model"] == "gpt-test"

    list_result = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "swarmctl.py"),
            "--db", db_path,
            "models", "list",
            "--role", "scanner",
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert list_result.returncode == 0, list_result.stderr
    listed = json.loads(list_result.stdout)["profiles"]
    assert any(p["provider"] == "codex" and p["is_default"] for p in listed)

    event_result = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "swarmctl.py"),
            "--db", db_path,
            "event",
            "--run-id", run_id,
            "--type", "client_message",
            "--source", "codex",
            "--content", "Codex client asked swarm for the current run summary.",
            "--update-summary",
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert event_result.returncode == 0, event_result.stderr
    event_payload = json.loads(event_result.stdout)
    assert event_payload["event_id"]
    assert any(e["source"] == "codex" for e in event_payload["summary"]["events"])

    summary_result = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "swarmctl.py"),
            "--db", db_path,
            "summary",
            "--run-id", run_id,
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert summary_result.returncode == 0, summary_result.stderr
    summary = json.loads(summary_result.stdout)
    assert "Codex client" in summary["summary"]

    db = SwarmDB(db_path)
    stored = db.fetch_one("SELECT conversation_summary FROM swarm_runs WHERE run_id=?", (run_id,))
    assert "Recent events:" in stored["conversation_summary"]
    db.close()
    print("  ✅ swarmctl.py exposes model config and run summaries")


async def test_swarm_worker_executes_market_task():
    """测试 worker 从任务市场领取、执行、capture、complete"""
    print("\n=== Test: Swarm Worker Loop ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    task_id = publish_work_task(
        db,
        run_id,
        "analyze",
        "analyst",
        "Analyze a SQL injection finding from the shared market",
        context_entry_ids=[],
        source_agent="scanner-seed",
        intent="attack",
        priority=90,
    )

    def executor(task, context):
        assert task["task_id"] == task_id
        assert "Task" in context
        return {
            "content": (
                "Worker analysis confirms CVE-2024-9999 style SQL injection. "
                "The endpoint /api/users?id=1 concatenates input directly into SQL, "
                "so UNION SELECT can enumerate user records. Use parameterized queries."
            ),
            "tags": ["worker_loop", "sql_injection"],
            "intent": "attack",
            "token_cost": 42,
        }

    worker = SwarmWorker(db, run_id, "analyst-worker-001", "analyst", executor=executor)
    result = await worker.run_once()
    assert result and result.status == "completed"
    assert result.captured_entry_id

    task = db.fetch_one("SELECT status, agent_id, token_cost FROM agent_tasks WHERE task_id=?", (task_id,))
    assert task["status"] == "completed"
    assert task["agent_id"] == "analyst-worker-001"
    assert task["token_cost"] >= 42

    entry = db.fetch_one(
        "SELECT source_agent, source_task_id, tags FROM knowledge_entries WHERE id=?",
        (result.captured_entry_id,),
    )
    assert entry["source_agent"] == "analyst-worker-001"
    assert entry["source_task_id"] == task_id
    assert "worker_loop" in json.loads(entry["tags"])

    heartbeat = db.fetch_one("SELECT current_task_id, load_score FROM agent_heartbeats WHERE agent_id=?", ("analyst-worker-001",))
    assert heartbeat["current_task_id"] is None
    assert heartbeat["load_score"] == 0.0

    db.close()
    print("  ✅ worker claims, executes, captures, and completes market work")


async def test_worker_normalizes_task_result_intent():
    """测试 worker/capture 会把 run/task intent 映射为合法 knowledge_intent"""
    print("\n=== Test: Worker Intent Normalization ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    task_id = publish_work_task(
        db,
        run_id,
        "scan",
        "scanner",
        "Map authorized surface for intent normalization test",
        context_entry_ids=[],
        source_agent="system",
        intent="recon",
        priority=80,
    )

    def executor(task, context):
        return {
            "content": (
                "Scanner observed demo.example has HTTPS service metadata and "
                "documented endpoint structure. This is simulated recon output "
                "with concrete fields for capture normalization testing."
            ),
            "intent": "recon",
            "tags": ["intent_normalization"],
            "token_cost": 1,
        }

    worker = SwarmWorker(db, run_id, "scanner-intent-001", "scanner", executor=executor)
    result = await worker.run_once()
    assert result and result.status == "completed"
    assert result.captured_entry_id

    entry = db.fetch_one(
        "SELECT knowledge_intent, tags FROM knowledge_entries WHERE id=?",
        (result.captured_entry_id,),
    )
    assert entry["knowledge_intent"] == "enumerate"
    assert "intent_normalization" in json.loads(entry["tags"])

    task = db.fetch_one("SELECT status FROM agent_tasks WHERE task_id=?", (task_id,))
    assert task["status"] == "completed"

    db.close()
    print("  ✅ worker maps recon/report/analyze style intents before capture")


def test_agent_worker_cli_manual_claim_complete():
    """测试 CLI claim-only + complete-task 手动闭环"""
    print("\n=== Test: Agent Worker CLI ===")
    db_path = "/tmp/test_agent_worker_cli.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    db = SwarmDB(db_path)
    db.init()
    run_id = create_test_run(db)
    task_id = publish_work_task(
        db,
        run_id,
        "report",
        "reporter",
        "Prepare rolling report from current findings",
        context_entry_ids=[],
        source_agent="system",
        intent="report",
        priority=60,
    )
    db.close()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    claim = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "agent_worker.py"),
            "--db", db_path,
            "--run-id", run_id,
            "--agent", "reporter-cli-001",
            "--role", "reporter",
            "--claim-only",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert claim.returncode == 0, claim.stderr
    claimed = json.loads(claim.stdout)
    assert claimed["task"]["task_id"] == task_id
    assert claimed["model_profile"]["role"] == "reporter"
    assert claimed["model_profile"]["provider"] == "client"

    complete = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "agent_worker.py"),
            "--db", db_path,
            "--run-id", run_id,
            "--agent", "reporter-cli-001",
            "--role", "reporter",
            "--complete-task-id", task_id,
            "--client-source", "claude",
            "--content",
            (
                "Rolling report updated with SQL injection evidence, impact, "
                "reproduction outline, affected endpoint, and recommended remediation."
            ),
            "--tags", "report,worker_cli",
            "--intent", "understand",
            "--token-cost", "17",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert complete.returncode == 0, complete.stderr
    completed = json.loads(complete.stdout)
    assert completed["status"] == "completed"
    assert completed["captured_entry_id"]
    assert completed["event_id"]
    assert completed["model_profile"]["role"] == "reporter"

    db = SwarmDB(db_path)
    row = db.fetch_one("SELECT status, token_cost FROM agent_tasks WHERE task_id=?", (task_id,))
    assert row["status"] == "completed"
    assert row["token_cost"] >= 17
    entry = db.fetch_one("SELECT tags FROM knowledge_entries WHERE id=?", (completed["captured_entry_id"],))
    assert "worker_cli" in json.loads(entry["tags"])
    event = db.fetch_one(
        "SELECT source, event_type FROM swarm_conversation_events WHERE event_id=?",
        (completed["event_id"],),
    )
    assert event["source"] == "claude"
    assert event["event_type"] == "task_completed"
    db.close()
    print("  ✅ agent_worker.py supports manual claim and complete")


def test_capture_triggers_spawn():
    """测试 capture() 自动触发 spawn 请求"""
    print("\n=== Test: Capture → Auto-spawn ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    # 注册 scanner agent
    lc = AgentLifecycle(db, "test-scanner-001", run_id)
    lc.register(role="scanner")

    # 创建 dummy task 满足 capture 的 FK 约束
    db.execute(
        "INSERT INTO agent_tasks (task_id, run_id, agent_id, task_type, status) "
        "VALUES ('task-001', ?, 'test-scanner-001', 'scan', 'running')",
        (run_id,),
    )
    db.conn.commit()

    # 模拟 capture 一个 vulnerability 发现
    ctx = CaptureContext(
        source=CaptureSource.TASK_RESULT,
        content="发现 SQL injection 漏洞在 /api/users?id= 参数。"
                " 攻击者可注入 UNION SELECT 语句提取所有用户数据。"
                " 建议使用参数化查询修复此漏洞。"
                " CVE pattern: similar to CVE-2023-1234",
        source_agent="test-scanner-001",
        source_run_id=run_id,
        source_task_id="task-001",
        metadata={"task_type": "scan", "tool": "sqlmap", "intent": "attack"},
    )

    entry_id = capture(db, ctx)
    print(f"  capture result: {entry_id[:8] if entry_id else 'filtered'}")

    # 验证 spawn 请求是否被自动创建
    pending = poll_spawn_requests(db, run_id)
    spawned_roles = [r["requested_role"] for r in pending]
    print(f"  auto-spawn roles: {spawned_roles}")

    assert "exploiter" in spawned_roles, "capture should auto-trigger exploiter spawn"
    assert "analyst" in spawned_roles, "capture should fan out analyst capacity request"
    assert "reporter" in spawned_roles, "capture should fan out reporter capacity request"

    market_tasks = poll_work_tasks(db, run_id=run_id, status="pending", limit=10)
    market_roles = {t["required_role"] for t in market_tasks}
    assert {"analyst", "exploiter", "reporter"}.issubset(market_roles)
    assert len(market_tasks) >= 3
    print("  ✅ capture fans out into parallel market tasks and spawn signals")

    db.close()
    print("=== Capture Auto-spawn: DONE ===")


def test_extractor_sql_generation_sqlite():
    """测试批量提取 SQL 与 SQLite schema 兼容"""
    print("\n=== Test: Extractor SQLite SQL ===")
    db = setup_test_db()

    assert extract_knowledge_from_text("too short") == []

    entry = ExtractedKnowledge(
        content=(
            "sqlmap confirmed CVE-2024-9999 SQL injection in /api/users?id=1. "
            "The owner's table can be extracted with UNION SELECT because the "
            "parameter is concatenated directly into SQL."
        ),
        title="SQLite insert owner's check",
        knowledge_type="vulnerability",
        domain="security",
        level=2,
        tags=["sqlmap", "owner's_tool"],
        confidence=0.8,
        source_ref={"url": "https://example.test/report", "quote": "owner's note"},
        knowledge_intent="attack",
    )
    formatted = format_extraction_for_insert([entry], source_agent="extractor-test")
    assert formatted[0]["content_hash"]

    insert_sql = generate_insert_sql(formatted)
    lineage_sql = generate_lineage_sql(formatted)
    for forbidden in ("::jsonb", "ARRAY[", "ON CONFLICT DO NOTHING"):
        assert forbidden not in insert_sql
        assert forbidden not in lineage_sql

    db.conn.executescript(insert_sql)
    db.conn.executescript(lineage_sql)

    row = db.fetch_one("SELECT tags, content_hash FROM knowledge_entries WHERE id=?", (formatted[0]["id"],))
    assert row and row["content_hash"] == formatted[0]["content_hash"]
    assert "owner's_tool" in json.loads(row["tags"])

    lineage = db.fetch_one("SELECT source_ref FROM knowledge_lineage WHERE knowledge_id=?", (formatted[0]["id"],))
    assert lineage and json.loads(lineage["source_ref"])["quote"] == "owner's note"

    db.close()
    print("  ✅ extractor SQL executes on SQLite")


def test_capture_cli_merges_tags_before_store():
    """测试 CLI tags 在 capture 分类阶段生效，而不是入库后覆盖"""
    print("\n=== Test: Capture CLI Tags ===")
    db_path = "/tmp/test_capture_cli_tags.db"
    if os.path.exists(db_path):
        os.remove(db_path)

    db = SwarmDB(db_path)
    db.init()
    existing_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent,
            domain, knowledge_intent, tags)
           VALUES (?, 2, 'vulnerability', 'existing manual tag evidence',
                   'existing manual tag', 'seed-agent',
                   'security', 'attack', ?)""",
        (existing_id, json.dumps(["manual_tag"])),
    )
    db.conn.commit()
    db.close()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    content = (
        "sqlmap confirmed CVE-2024-9999 SQL injection in /api/users?id=1. "
        "Evidence shows UNION SELECT can extract user records because input is "
        "concatenated directly into SQL, so parameterized queries are required."
    )
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "capture.py"),
            "--db", db_path,
            "--content", content,
            "--agent", "cli-agent",
            "--source", "task_result",
            "--tags", "manual_tag,SQL Injection",
            "--intent", "attack",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CAPTURED:" in result.stdout

    db = SwarmDB(db_path)
    row = db.fetch_one(
        "SELECT id, tags FROM knowledge_entries WHERE source_agent='cli-agent' ORDER BY created_at DESC LIMIT 1"
    )
    assert row
    tags = json.loads(row["tags"])
    assert "manual_tag" in tags
    assert "sql_injection" in tags
    assert "sqlmap" in tags
    assert "cve-2024-9999" in tags

    corroboration = db.fetch_one(
        """SELECT source_ref FROM knowledge_lineage
           WHERE knowledge_id = ? AND source_type = 'cross_agent_validation'""",
        (existing_id,),
    )
    assert corroboration, "manual CLI tag should participate in auto-corroboration"
    db.close()

    fresh_db_path = "/tmp/test_capture_cli_fresh.db"
    if os.path.exists(fresh_db_path):
        os.remove(fresh_db_path)
    fresh_result = subprocess.run(
        [
            sys.executable,
            os.path.join(repo, "scripts", "capture.py"),
            "--db", fresh_db_path,
            "--content", content,
            "--agent", "fresh-cli-agent",
            "--source", "task_result",
            "--tags", "fresh_tag",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fresh_result.returncode == 0, fresh_result.stderr
    fresh_db = SwarmDB(fresh_db_path)
    count = fresh_db.fetch_one("SELECT COUNT(*) AS c FROM knowledge_entries")["c"]
    assert count == 1
    fresh_db.close()

    print("  ✅ CLI tags are merged before enrichment/corroboration")


def test_capture_preserves_filtered_raw_events_for_handoff():
    """测试低信号内容不进 KB 时仍进入 raw_agent_events（数据无损），但按安全契约
    （审计 A4, 2026-08-11）不注入 worker context——被过滤内容仍是不可信文本。"""
    print("\n=== Test: Lossless Raw Capture ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    AgentLifecycle(db, "raw-agent-001", run_id).register(role="analyst")

    short_ctx = CaptureContext(
        source=CaptureSource.CONVERSATION,
        content="too short",
        source_agent="raw-agent-001",
        source_run_id=run_id,
        metadata={"phase": "raw-test"},
    )
    entry_id = capture(db, short_ctx)
    assert entry_id is None

    raw = db.fetch_one(
        "SELECT capture_status, filter_reason, content FROM raw_agent_events WHERE run_id=?",
        (run_id,),
    )
    assert raw["capture_status"] == "filtered"
    assert raw["filter_reason"] == "content_too_short"
    assert raw["content"] == "too short"

    task_id = publish_work_task(
        db,
        run_id,
        "analyze",
        "analyst",
        "Use raw handoff event",
        source_agent="raw-agent-001",
    )
    task = dict(db.fetch_one("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)))
    context = build_task_context(db, task)
    # 安全契约（审计 A4）: filtered 事件保留在 raw_agent_events 表（数据无损），
    # 但不注入 worker context——被过滤内容仍是不可信文本，且对任务无信息价值
    assert "Recent Raw Handoff Events" not in context
    assert "too short" not in context

    untrusted_forced_ctx = CaptureContext(
        source=CaptureSource.CONVERSATION,
        content="forced",
        source_agent="raw-agent-001",
        source_run_id=run_id,
        metadata={"force_capture": True, "title": "untrusted forced short capture"},
    )
    untrusted_forced_id = capture(db, untrusted_forced_ctx)
    assert untrusted_forced_id is None
    assert "force_capture" not in untrusted_forced_ctx.metadata

    forced_ctx = CaptureContext(
        source=CaptureSource.TASK_RESULT,
        content="forced",
        source_agent="raw-agent-001",
        source_run_id=run_id,
        metadata={"force_capture": True, "title": "forced short capture"},
    )
    forced_id = capture(db, forced_ctx)
    assert forced_id
    forced = db.fetch_one("SELECT title FROM knowledge_entries WHERE id=?", (forced_id,))
    assert forced["title"] == "forced short capture"

    db.close()
    print("  ✅ filtered findings are preserved as raw handoff events")


async def test_worker_artifact_verification_blocks_missing_files():
    """测试 worker 不信任 agent 的文件声明，缺失 artifact 会使任务失败"""
    print("\n=== Test: Worker Artifact Verification ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    task_id = publish_work_task(
        db,
        run_id,
        "report",
        "reporter",
        "Produce a report artifact",
        source_agent="system",
        priority=80,
    )

    missing_path = "/tmp/swarm-artifact-does-not-exist.md"
    if os.path.exists(missing_path):
        os.remove(missing_path)

    def executor(task, context):
        return {
            "content": "Reporter claims a final report was written.",
            "artifacts": [missing_path],
        }

    worker = SwarmWorker(db, run_id, "reporter-artifact-001", "reporter", executor=executor)
    result = await worker.run_once()
    assert result and result.status == "failed"
    assert result.error == "artifact verification failed"

    task = db.fetch_one("SELECT status FROM agent_tasks WHERE task_id=?", (task_id,))
    assert task["status"] == "failed"
    artifact = db.fetch_one(
        "SELECT status, declared_path FROM agent_artifacts WHERE task_id=?",
        (task_id,),
    )
    assert artifact["status"] == "missing"
    assert artifact["declared_path"] == missing_path

    db.close()
    print("  ✅ missing required artifacts fail the task")


def test_artifact_verifier_records_verified_files():
    """测试 parent runtime 能 stat/hash 可见 artifact"""
    print("\n=== Test: Artifact Verification Success ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    task_id = publish_work_task(db, run_id, "report", "reporter", "Verify artifact", source_agent="system")
    path = f"/tmp/swarm-artifact-{uuid.uuid4().hex}.txt"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("verified artifact content\n")

    result = verify_artifacts(
        db,
        run_id=run_id,
        task_id=task_id,
        agent_id="reporter-artifact-002",
        artifacts=[path],
    )
    assert result["ok"]
    assert result["verified"][0]["status"] == "verified"
    assert result["verified"][0]["size_bytes"] > 0
    stored = db.fetch_one("SELECT status, sha256 FROM agent_artifacts WHERE task_id=?", (task_id,))
    assert stored["status"] == "verified"
    assert len(stored["sha256"]) == 64

    os.remove(path)
    db.close()
    print("  ✅ verified artifacts are recorded with sha256")


def test_hermes_spawn_handler():
    """测试 Hermes handler 模板格式化 + delegate 返回 agent_id"""
    print("\n=== Test: Hermes Spawn Handler ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    calls = []

    def delegate_fn(goal: str, context: str):
        calls.append({"goal": goal, "context": context})
        return {"agent_id": "delegate-agent-001"}

    req = {
        "request_id": str(uuid.uuid4()),
        "run_id": run_id,
        "requested_role": "scanner",
        "reason": "Scan target for open services",
        "chain_depth": 0,
        "max_chain_depth": 3,
        "parent_task_id": "task-hermes-001",
    }
    handler = HermesSpawnHandler(db, delegate_fn=delegate_fn)
    agent_id = asyncio.run(handler(req, "KB context"))

    assert agent_id == "delegate-agent-001"
    assert calls and "--run-id" in calls[0]["goal"] and "--task-id" in calls[0]["goal"]
    row = db.fetch_one("SELECT role, status FROM agent_profiles WHERE agent_id=?", (agent_id,))
    assert row["role"] == "scanner" and row["status"] == "active"

    missing_delegate = HermesSpawnHandler(db)
    missing = asyncio.run(missing_delegate.create_agent(req, "KB context"))
    assert missing is None

    db.close()
    print("  ✅ Hermes handler formats goal and requires delegate_fn")


def test_governance_decay_counter_examples():
    """测试反例达到阈值时治理衰减不崩溃并标记 stale"""
    print("\n=== Test: Governance Decay ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    entry_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent, source_run_id,
            domain, knowledge_intent, tags)
           VALUES (?, 3, 'mechanism', 'verified content with evidence because it matters',
                   'decay target', 'test-agent', ?, 'security', 'understand', '[]')""",
        (entry_id, run_id),
    )
    for _ in range(5):
        db.execute(
            "INSERT INTO counter_examples (id, knowledge_id, source_agent, description) VALUES (?, ?, 'test', 'counter')",
            (str(uuid.uuid4()), entry_id),
        )
    db.conn.commit()

    result = check_and_decay(db)
    assert result["decayed_entries"], "expected entry decay"
    row = db.fetch_one("SELECT status FROM knowledge_entries WHERE id=?", (entry_id,))
    assert row["status"] == "stale"

    db.close()
    print("  ✅ governance decay handles sqlite rows")


def test_ontology_discovery_persists():
    """测试本体共现发现返回 discovered 时确实落库"""
    print("\n=== Test: Ontology Discovery ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    before = db.fetch_one("SELECT COUNT(*) AS c FROM ontology_relations")["c"]

    for i in range(2):
        db.execute(
            """INSERT INTO knowledge_entries
               (id, level, knowledge_type, content, title, source_agent, source_run_id,
                domain, knowledge_intent, tags)
               VALUES (?, 1, 'observation', ?, 'nuclei-port-scan', 'test-agent', ?,
                       'security', 'enumerate', ?)""",
            (
                str(uuid.uuid4()),
                f"nuclei and port_scan co-occur in sample {i}",
                run_id,
                json.dumps(["nuclei", "port_scan"]),
            ),
        )
    db.conn.commit()

    result = discover_relations_from_cooccurrence(db)
    after = db.fetch_one("SELECT COUNT(*) AS c FROM ontology_relations")["c"]

    assert result["discovered"], "expected discovered ontology relation"
    assert after == before + len(result["discovered"])
    row = db.fetch_one(
        """SELECT source FROM ontology_relations
           WHERE from_concept_id='seed-nuclei' AND to_concept_id='seed-port-scan'
             AND relation_type='implements'"""
    )
    assert row["source"] == "inferred"

    db.close()
    print("  ✅ ontology discovery persists inferred relations")


def test_validation_queue_scoped_and_not_requeued():
    """测试验证队列按 run 过滤，处理后不会重复入队"""
    print("\n=== Test: Validation Queue ===")
    db = setup_test_db()
    run_id = create_test_run(db)
    other_run = create_test_run(db)

    def insert_vuln(run):
        entry_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO knowledge_entries
               (id, level, knowledge_type, content, title, source_agent, source_run_id,
                domain, knowledge_intent, tags, trust_vector)
               VALUES (?, 2, 'vulnerability', ?, 'validation target', 'test-agent', ?,
                       'security', 'attack', '[]',
                       '{"logic_soundness":0.9,"base_confidence":0.9,"cross_validation":0.8}')""",
            (
                entry_id,
                "CVE-2024-9999 at http://example.test with nuclei evidence",
                run,
            ),
        )
        db.execute(
            """INSERT INTO knowledge_lineage
               (knowledge_id, source_type, source_ref, extraction_method, confidence_contribution)
               VALUES (?, 'agent_execution', ?, 'agent_analysis', 0.9)""",
            (entry_id, json.dumps({"run": run})),
        )
        return entry_id

    first = insert_vuln(run_id)
    insert_vuln(other_run)
    db.conn.commit()

    result = auto_enqueue_validations(db, run_id)
    assert result["enqueued"] == 1
    assert result["hypotheses"] == 1
    queued_runs = [r["run_id"] for r in db.fetch_all("SELECT run_id FROM validation_queue")]
    assert queued_runs == [run_id]
    hypothesis_count = db.fetch_one("SELECT COUNT(*) AS c FROM finding_hypotheses WHERE run_id=?", (run_id,))["c"]
    assert hypothesis_count == 1

    processed = process_validation_queue(db)
    assert processed["processed"] == 1
    again = auto_enqueue_validations(db, run_id)
    assert again["enqueued"] == 0
    row = db.fetch_one("SELECT validation_count FROM knowledge_entries WHERE id=?", (first,))
    assert row["validation_count"] == 1

    db.close()
    print("  ✅ validation queue is run-scoped and one-shot")


def test_bounty_hypothesis_gates_and_negative_knowledge():
    """测试赏金候选默认是假设，门控全过才验证，失败门控沉淀为负知识"""
    print("\n=== Test: Bounty Knowledge Loop ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    validated_entry_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent, source_run_id,
            domain, knowledge_intent, tags, trust_vector)
           VALUES (?, 2, 'vulnerability', ?, 'candidate auth bypass', 'analyst-a', ?,
                   'security', 'attack', ?,
                   '{"logic_soundness":0.8,"base_confidence":0.75,"cross_validation":0.4}')""",
        (
            validated_entry_id,
            "WCF method accepts attacker-controlled URL and executes update flow with evidence.",
            run_id,
            json.dumps(["auth_bypass", "ssrf", "oem_agent"]),
        ),
    )

    hypothesis = create_finding_hypothesis(
        db,
        validated_entry_id,
        target_id="oem-agent",
        program="vendor-bounty",
        vulnerability_class="auth-bypass",
        severity="high",
        scope_status="in_scope",
        reachability="low_priv",
        expected_payout=5000,
        estimated_hours=20,
        competition_factor=0.8,
        rationale="OEM agent is in scope and runs privileged local services.",
    )
    assert hypothesis["validation_status"] == "hypothesis"
    assert len(hypothesis["gates"]) == 6

    ranked = rank_hypotheses_by_roi(db)
    assert ranked and ranked[0]["hypothesis_id"] == hypothesis["hypothesis_id"]
    assert ranked[0]["roi_score"] == 200.0

    final = None
    for gate in ("poc_exists", "clean_repro", "impactful", "low_priv_reachable", "in_scope", "deduplicated"):
        final = record_gate_result(
            db,
            hypothesis["hypothesis_id"],
            gate,
            "pass",
            evidence=f"{gate} verified in clean test environment",
            verified_by="validator-a",
        )

    assert final["status"] == "validated"
    row = db.fetch_one("SELECT level, validation_count FROM knowledge_entries WHERE id=?", (validated_entry_id,))
    assert row["level"] >= 3
    assert row["validation_count"] == 1

    refuted_entry_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent, source_run_id,
            domain, knowledge_intent, tags, trust_vector)
           VALUES (?, 2, 'vulnerability', ?, 'candidate named pipe bug', 'analyst-b', ?,
                   'security', 'attack', ?,
                   '{"logic_soundness":0.7,"base_confidence":0.7,"cross_validation":0.2}')""",
        (
            refuted_entry_id,
            "Named pipe looked exploitable during SYSTEM-context analysis.",
            run_id,
            json.dumps(["named_pipe", "lpe", "oem_agent"]),
        ),
    )
    refuted = create_finding_hypothesis(
        db,
        refuted_entry_id,
        target_id="oem-agent",
        program="vendor-bounty",
        vulnerability_class="lpe",
        severity="medium",
        expected_payout=2500,
        estimated_hours=16,
    )
    failed = record_gate_result(
        db,
        refuted["hypothesis_id"],
        "low_priv_reachable",
        "fail",
        evidence="Standard user cannot open the named pipe; only SYSTEM can reach it.",
        verified_by="validator-b",
    )

    assert failed["status"] == "negative_knowledge"
    negatives = get_negative_knowledge(db, target_id="oem-agent", reason_type="privilege_unreachable")
    assert len(negatives) == 1
    assert "Standard user" in negatives[0]["details"]
    ce = db.fetch_one("SELECT COUNT(*) AS c FROM counter_examples WHERE knowledge_id=?", (refuted_entry_id,))
    assert ce["c"] == 1

    db.close()
    print("  ✅ hypothesis gates promote validated findings and preserve negative knowledge")


def test_transitive_inference_persists_only_new_edges():
    """测试传递推理不返回自环或假新增"""
    print("\n=== Test: Ontology Transitive Inference ===")
    db = setup_test_db()
    a_id, b_id, c_id = "test-concept-a", "test-concept-b", "test-concept-c"
    for cid, name in ((a_id, "test_a"), (b_id, "test_b"), (c_id, "test_c")):
        db.execute(
            "INSERT INTO ontology_concepts (concept_id, concept_name, concept_type, source) VALUES (?, ?, 'technique', 'manual')",
            (cid, name),
        )
    db.execute(
        "INSERT INTO ontology_relations (relation_id, from_concept_id, to_concept_id, relation_type, source) VALUES (?, ?, ?, 'depends_on', 'manual')",
        (str(uuid.uuid4()), a_id, b_id),
    )
    db.execute(
        "INSERT INTO ontology_relations (relation_id, from_concept_id, to_concept_id, relation_type, source) VALUES (?, ?, ?, 'depends_on', 'manual')",
        (str(uuid.uuid4()), b_id, c_id),
    )
    db.conn.commit()

    inferred = infer_transitive_relations(db, "depends_on")
    assert inferred == [{"from": a_id[:8], "to": c_id[:8]}]
    inferred_again = infer_transitive_relations(db, "depends_on")
    assert inferred_again == []

    db.close()
    print("  ✅ transitive inference reports only persisted new edges")


async def test_orchestrator_work_market_expands_missing_roles():
    """测试 Orchestrator 根据任务市场角色缺口触发扩容"""
    print("\n=== Test: Orchestrator Work Market ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    AgentLifecycle(db, "scanner-live", run_id).register(role="scanner")
    publish_work_task(
        db, run_id, "analyze", "analyst",
        "Analyst backlog from shared market",
        context_entry_ids=["entry-market"],
        source_agent="scanner-live",
        priority=85,
    )

    orch = SwarmOrchestrator(db)
    await orch._tick_work_market(run_id)

    pending_spawn = poll_spawn_requests(db, run_id)
    assert any(r["requested_role"] == "analyst" for r in pending_spawn)
    behavior = db.fetch_one(
        "SELECT description FROM swarm_behaviors WHERE run_id=? AND behavior_type='adaptation'",
        (run_id,),
    )
    assert behavior and "任务市场扩容" in behavior["description"]

    AgentLifecycle(db, "analyst-live", run_id).register(role="analyst")
    await orch._balance_load(run_id)
    task = db.fetch_one(
        "SELECT status, agent_id FROM agent_tasks WHERE run_id=? AND required_role='analyst'",
        (run_id,),
    )
    assert task["status"] == "running"
    assert task["agent_id"] == "analyst-live"

    db.close()
    print("  ✅ orchestrator expands missing roles and idle agents claim market work")


async def test_stigmergy_auto_spawn_from_vulnerability():
    """测试 stigmergy auto-spawn: vulnerability知识条目 → 自动生成 spawn_requests"""
    print("\n=== Test: Stigmergy Auto-Spawn from Vulnerability ===")
    from uuid import uuid4
    db = setup_test_db()
    run_id = create_test_run(db)

    entry_id = str(uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, title, content, knowledge_type, level, source_run_id, source_agent, status, created_at)
           VALUES (?, 'SQL Injection in login', 'Found SQLi vuln',
           'vulnerability', 3, ?, 'test-stigmergy', 'active', datetime('now'))""",
        (entry_id, run_id),
    )
    db.execute(
        """INSERT INTO knowledge_entries
           (id, title, content, knowledge_type, level, source_run_id, source_agent, status, created_at)
           VALUES (?, 'Open Redirect', 'Found open redirect',
           'observation', 3, ?, 'test-stigmergy', 'active', datetime('now'))""",
        (str(uuid4()), run_id),
    )
    db.conn.commit()

    orch = SwarmOrchestrator(db)

    async def mock_handler(req: dict, context: str) -> str:
        agent_id = f"mock-{req['requested_role']}-{uuid4().hex[:6]}"
        lc = AgentLifecycle(db, agent_id, run_id)
        lc.register(role=req["requested_role"])
        return agent_id

    orch.set_spawn_handler(mock_handler)

    await orch._tick_stigmergy_spawn(run_id)

    spawns = db.fetch_all(
        "SELECT requested_role, reason FROM spawn_requests WHERE run_id=? AND status='pending'",
        (run_id,),
    )
    roles = {s["requested_role"] for s in spawns}
    assert "analyst" in roles, f"expected analyst spawn, got {roles}"
    print(f"  ✅ auto-spawned {len(spawns)} requests for roles: {roles}")

    # 再次调用 stigmergy tick — 不应重复 spawn
    await orch._tick_stigmergy_spawn(run_id)
    second_run = db.fetch_all(
        "SELECT requested_role FROM spawn_requests WHERE run_id=? AND status='pending'",
        (run_id,),
    )
    assert len(second_run) == len(spawns), f"expected idempotent spawn, got {len(second_run)} vs {len(spawns)}"
    print("  ✅ idempotent — no duplicate spawns on second tick")

    db.close()


async def test_orchestrator_loop():
    """测试 Orchestrator 主循环（mock spawn handler）"""
    print("\n=== Test: Orchestrator Loop ===")
    db = setup_test_db()
    run_id = create_test_run(db)

    spawned_agents = []

    async def mock_spawn_handler(req: dict, context: str) -> str:
        """Mock: 不调用 Claude API，直接返回 agent_id"""
        agent_id = f"mock-{req['requested_role']}-{uuid.uuid4().hex[:6]}"
        # 注册到 agent_profiles 满足 FK 约束
        lc = AgentLifecycle(db, agent_id, run_id)
        lc.register(role=req['requested_role'])
        spawned_agents.append({"role": req["requested_role"], "agent_id": agent_id, "reason": req["reason"]})
        return agent_id

    orch = SwarmOrchestrator(db)
    orch.set_spawn_handler(mock_spawn_handler)

    # 注册 3 个 Agent
    for role in ("scanner", "analyst", "reporter"):
        agent_id = f"test-{role}-001"
        lc = AgentLifecycle(db, agent_id, run_id)
        lc.register(role=role)

    # 手动创建 spawn 请求
    request_spawn(db, run_id, "test-scanner-001", "exploiter",
                  reason="发现漏洞需要利用", priority=80)
    request_spawn(db, run_id, "test-analyst-001", "exploiter",
                  reason="分析确认需要利用", priority=60)

    # 在后台运行 Orchestrator 10 秒
    async def run_short():
        await orch.run_loop(run_id, tick_interval=1.0)

    task = asyncio.create_task(run_short())

    # 等待 spawn 被处理
    await asyncio.sleep(POLL_SPAWN_SEC + 2)
    orch.stop()
    await task

    print(f"  spawned agents: {len(spawned_agents)}")
    for sa in spawned_agents:
        print(f"    - {sa['role']}: {sa['agent_id']} ({sa['reason'][:50]})")

    # 验证 spawn_requests 状态
    fulfilled = db.fetch_all(
        "SELECT request_id, requested_role, status FROM spawn_requests WHERE status='fulfilled'"
    )
    print(f"  fulfilled requests: {len(fulfilled)}")
    for f in fulfilled:
        print(f"    - {f['requested_role']}: {f['status']}")

    # 验证 behavior 日志
    behaviors = db.fetch_all("SELECT behavior_type, description FROM swarm_behaviors WHERE run_id=?", (run_id,))
    print(f"  behavior logs: {len(behaviors)}")
    for b in behaviors:
        print(f"    - [{b['behavior_type']}] {b['description'][:80]}")

    assert len(fulfilled) > 0, "Expected at least one spawn fulfilled"
    assert len(behaviors) > 0, "Expected at least one behavior logged"
    print("  ✅ Orchestrator processed spawn requests")
    print("  ✅ Behavior logged")

    # 测试心跳清理：标记一个 Agent 为"僵尸"
    db.execute(
        "UPDATE agent_heartbeats SET last_beat = datetime('now', '-120 seconds') WHERE agent_id = 'test-scanner-001'"
    )
    db.conn.commit()

    # 运行一次清理 tick
    stale = cleanup_stale_agents(db, timeout_sec=90)
    print(f"  stale agents cleaned: {stale}")
    assert len(stale) >= 1, f"Expected at least 1 stale agent, got {len(stale)}"

    # 验证 profile 被标记为 deprecated
    row = db.fetch_one("SELECT status FROM agent_profiles WHERE agent_id='test-scanner-001'")
    assert row["status"] == "deprecated", f"Expected deprecated, got {row['status']}"
    print("  ✅ stale agent cleanup works")

    db.close()
    print("=== Orchestrator Loop: ALL PASSED ===")


# ── Main ──

def main():
    """运行所有测试"""
    print("=" * 60)
    print("Swarm Knowledge Base — 集成测试")
    print("=" * 60)

    test_lifecycle()
    test_spawn()
    test_work_market_claims_are_atomic()
    test_work_market_generations_follow_parent_tasks()
    test_model_profiles_are_swarm_owned()
    test_seeded_swarm_run_publishes_market_tasks()
    asyncio.run(test_swarm_runner_executes_multi_worker_pool())
    test_start_swarm_cli_outputs_seeded_run()
    test_client_api_submits_task_and_fetches_result()
    test_swarmctl_cli_task_submit_status_result()
    test_swarmctl_cli_models_event_summary()
    asyncio.run(test_swarm_worker_executes_market_task())
    asyncio.run(test_worker_normalizes_task_result_intent())
    test_agent_worker_cli_manual_claim_complete()
    test_capture_triggers_spawn()
    test_extractor_sql_generation_sqlite()
    test_capture_cli_merges_tags_before_store()
    test_capture_preserves_filtered_raw_events_for_handoff()
    asyncio.run(test_worker_artifact_verification_blocks_missing_files())
    test_artifact_verifier_records_verified_files()
    test_hermes_spawn_handler()
    test_governance_decay_counter_examples()
    test_ontology_discovery_persists()
    test_validation_queue_scoped_and_not_requeued()
    test_bounty_hypothesis_gates_and_negative_knowledge()
    test_transitive_inference_persists_only_new_edges()
    asyncio.run(test_orchestrator_work_market_expands_missing_roles())

    # Orchestrator 需要 asyncio
    asyncio.run(test_orchestrator_loop())

    # Stigmergy auto-spawn
    asyncio.run(test_stigmergy_auto_spawn_from_vulnerability())

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
