"""
G2 replay_verifier 单元测试 (2026-08-11)

覆盖: 安全降级(未配置/未授权) / URL 提取 / 授权匹配(含子域) /
可达性复现(本地 http.server) / 不可达判定 / 集成到 _auto_verify 的 verdict 路径。

运行: .venv/bin/python -m pytest tests/test_replay_verifier.py -q
"""
from __future__ import annotations

import http.server
import socketserver
import threading

import pytest

from src.governance.replay_verifier import (
    ReplayUnverifiable,
    build_replay_verifier,
    extract_http_targets,
)


@pytest.fixture()
def local_http():
    """在 127.0.0.1 起一个返回 200 的 HTTP 服务, 返回 (host, port)。"""
    handler = http.server.SimpleHTTPRequestHandler

    class Quiet(handler):
        def log_message(self, format, *args):  # 静默访问日志
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Quiet) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield "127.0.0.1", httpd.server_address[1]
        httpd.shutdown()
        thread.join(timeout=2)


def test_extract_http_targets():
    content = "发现 http://example.com/path 和 https://sub.example.com:8443/x 以及 10.0.0.5"
    hosts = extract_http_targets(content)
    assert "example.com" in hosts
    assert "sub.example.com" in hosts
    assert "10.0.0.5" not in hosts, "裸 IP 无 scheme 不算 HTTP 目标"


def test_verifier_disabled_raises():
    """未配置授权目标 -> 恒 ReplayUnverifiable (安全降级, 不对外请求)"""
    verifier = build_replay_verifier(None)
    with pytest.raises(ReplayUnverifiable):
        verifier("k1", "found vuln at http://example.com/x")
    verifier2 = build_replay_verifier(set())
    with pytest.raises(ReplayUnverifiable):
        verifier2("k1", "found vuln at http://example.com/x")


def test_verifier_no_url_raises():
    verifier = build_replay_verifier({"example.com"})
    with pytest.raises(ReplayUnverifiable):
        verifier("k1", "analyst observed unusual behavior in module X")


def test_verifier_unauthorized_host_raises():
    """目标不在授权集 -> ReplayUnverifiable, 绝不误判"""
    verifier = build_replay_verifier({"trusted.example.com"})
    with pytest.raises(ReplayUnverifiable):
        verifier("k1", "found issue at http://evil.example.org/x")


def test_verifier_subdomain_match():
    assert extract_http_targets("http://api.example.com/x") == ["api.example.com"]
    verifier = build_replay_verifier({"example.com"})
    # 子域授权匹配 + 本地不可达 -> 不抛未授权异常, 而是走不可达/无法判定路径
    with pytest.raises(ReplayUnverifiable) as excinfo:
        verifier("k1", "found issue at http://api.example.com:59999/x")
    assert "未授权" not in str(excinfo.value)


def test_verifier_reachable_confirmed(local_http):
    """授权 + 可达 -> (True, evidence)"""
    host, port = local_http
    verifier = build_replay_verifier({host})
    ok, evidence = verifier("k1", f"service at http://{host}:{port}/index.html returns 200")
    assert ok is True
    assert "HTTP" in evidence and "连通性复现确认" in evidence


def test_verifier_unreachable_claimed_refuted(local_http):
    """授权 + 不可达 + content 声称可达 -> (False, evidence) 真反证"""
    host, _port = local_http  # 服务活着, 但我们探测一个未监听端口
    verifier = build_replay_verifier({host})
    ok, evidence = verifier(
        "k1", f"http://{host}:59998/ is open and reachable, HTTP 200"
    )
    assert ok is False
    assert "请求失败" in evidence or "拒绝" in evidence or "失败" in evidence


def test_verifier_unreachable_unclaimed_raises(local_http):
    """授权 + 不可达 + 未声称可达 -> ReplayUnverifiable (不武断)"""
    host, _port = local_http
    verifier = build_replay_verifier({host})
    with pytest.raises(ReplayUnverifiable):
        verifier("k1", f"check http://{host}:59997/nope please")


def test_verifier_integrated_with_auto_verify(db, run_id, local_http):
    """集成: HIGH 条目 + 授权可达 -> process_validation_queue 判 confirmed"""
    from src.governance.verification import auto_enqueue_validations, process_validation_queue

    host, port = local_http
    entry_id = str(__import__("uuid").uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent, source_run_id,
            domain, knowledge_intent, tags, trust_vector)
           VALUES (?, 3, 'vulnerability', ?, 'target', 'test-agent', ?,
                   'security', 'attack', '[]', ?)""",
        (entry_id, f"service at http://{host}:{port}/index.html returns 200", run_id,
         '{"logic_soundness":0.9,"base_confidence":0.9,"cross_validation":0.8}'),
    )
    db.conn.commit()
    auto_enqueue_validations(db, run_id)
    verifier = build_replay_verifier({host})
    processed = process_validation_queue(db, replay_verifier=verifier)
    assert processed["confirmed"] == 1, processed
    row = db.fetch_one(
        "SELECT status, verdict FROM validation_queue WHERE knowledge_id=?", (entry_id,)
    )
    assert row["verdict"] == "confirmed"
    assert row["status"] == "verified"


def test_verifier_high_without_enabled_stays_inconclusive(db, run_id, local_http):
    """集成: HIGH 条目 + 未启用 verifier -> inconclusive (G2 安全降级)"""
    from src.governance.verification import auto_enqueue_validations, process_validation_queue

    host, port = local_http
    entry_id = str(__import__("uuid").uuid4())
    db.execute(
        """INSERT INTO knowledge_entries
           (id, level, knowledge_type, content, title, source_agent, source_run_id,
            domain, knowledge_intent, tags, trust_vector)
           VALUES (?, 3, 'vulnerability', ?, 'target', 'test-agent', ?,
                   'security', 'attack', '[]', ?)""",
        (entry_id, f"service at http://{host}:{port}/index.html returns 200", run_id,
         '{"logic_soundness":0.9,"base_confidence":0.9,"cross_validation":0.8}'),
    )
    db.conn.commit()
    auto_enqueue_validations(db, run_id)
    processed = process_validation_queue(db, replay_verifier=None)
    assert processed["confirmed"] == 0
    assert processed["inconclusive"] == 1, processed
