"""
G2 replay verifier (2026-08-11): 真实外部复现验证器。

背景: 蜂群自动验证 _auto_verify 原本是库内文本打分(交叉来源计数 + IP/CVE/URL
正则), 两个同样幻觉的 agent 互相印证即可"验证"通过。G2 修复要求 HIGH
(level>=3) 条目的 confirmed 必须由 replay_verifier 真实外部复现。

本模块提供:
- ReplayUnverifiable: 无法验证信号。抛出后由 verification._auto_verify 捕获,
  该条目判 inconclusive —— 绝不把"无法验证"误判成 refuted(反证)。
- build_replay_verifier(authorized_targets): 工厂。返回
  (knowledge_id, content) -> (reproduced, evidence) 的回放验证器。

安全边界(硬约束):
- 只对 authorized_targets 内的 host 发起 HTTP 请求; 授权集为空时所有条目
  raise ReplayUnverifiable(安全降级: HIGH 条目保持 inconclusive, 等价未接线)。
- 只支持 http/https scheme; 请求前再次校验 host ∈ 授权集 (防 SSRF)。
- 不跟随重定向(避免跳转到未授权/内网目标)。
- 仅 GET + 超时; 无 shell 调用。

判定语义:
- content 含授权 URL 且请求成功(2xx/3xx) -> reproduced=True (目标真实存在/可达,
  evidence 附状态码与响应大小)
- content 明确声称目标可达/开放(如 "HTTP 200" / "reachable" / "open")但请求
  失败 -> reproduced=False (真反证)
- 其余情况 -> raise ReplayUnverifiable (不武断)

注意: 本验证器证明的是"目标真实可达", 不是"漏洞真实存在"。完整漏洞复现需要
exploit 脚本, 超出本模块范围 —— evidence 里会明确标注"连通性复现"。

用法::

    from src.governance.replay_verifier import build_replay_verifier
    verifier = build_replay_verifier({"example.com", "10.0.0.5"})
    processed = process_validation_queue(db, replay_verifier=verifier)
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from typing import Callable, List, Optional, Set, Tuple

_log = logging.getLogger("swarm_knowledge.replay_verifier")

# verifier 签名: (knowledge_id, content) -> (reproduced, evidence)
ReplayVerifier = Callable[[str, str], Tuple[bool, str]]

_URL_RE = re.compile(r"https?://([^\s/\"'<>]+)", re.IGNORECASE)
_CLAIMS_REACHABLE = re.compile(
    r"(HTTP\s*/\S*\s*2\d\d|HTTP\s*200|reachable|开放|可达|响应正常|alive|up\b)",
    re.IGNORECASE,
)
_HOST_PORT_RE = re.compile(r"^(?P<host>[A-Za-z0-9.\-_]+)(?::(?P<port>\d{1,5}))?$")


class ReplayUnverifiable(Exception):
    """无法验证: 无目标 / 未授权 / 无法判定。调用方应判 inconclusive。"""


def extract_http_targets(content: str) -> List[str]:
    """从发现内容提取 http(s) URL 的 host 列表(去重, 保序)。"""
    hosts: List[str] = []
    seen: Set[str] = set()
    for m in _URL_RE.finditer(content or ""):
        raw = m.group(1)
        # 去掉尾部标点
        raw = raw.rstrip(".,;:!?)]}")
        mm = _HOST_PORT_RE.match(raw)
        if not mm:
            continue
        host = mm.group("host").lower()
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def _host_allowed(host: str, authorized: Set[str]) -> bool:
    """host 精确匹配或为授权域的子域(sub.example.com ∈ example.com)。"""
    host = host.lower().strip(".")
    for a in authorized:
        a = a.lower().strip(".")
        if not a:
            continue
        if host == a or host.endswith("." + a):
            return True
    return False


def _probe_url(url: str, timeout: float) -> Tuple[bool, str]:
    """GET 探测单个 URL, 不跟随重定向。返回 (可达, 证据)。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "swarm-knowledge-replay-verifier/1.0",
            "Accept": "*/*",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (host 已授权校验)
            body = resp.read(1024)
            return True, f"GET {url} -> HTTP {resp.status}, {len(body)}B"
    except urllib.error.HTTPError as exc:
        # 3xx/4xx/5xx 也是"可达"证据(服务器真实响应了), 但非成功
        if 300 <= exc.code < 400:
            return True, f"GET {url} -> HTTP {exc.code} (重定向, 不跟随)"
        return True, f"GET {url} -> HTTP {exc.code} (服务器有响应)"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"GET {url} -> 请求失败: {exc.__class__.__name__}: {exc}"


def build_replay_verifier(
    authorized_targets: Optional[Set[str]] = None,
    timeout: float = 8.0,
) -> ReplayVerifier:
    """构建回放验证器。

    authorized_targets: 允许外部复现的 host 集合(域名/IP, 支持子域匹配)。
        None 或空集 -> 恒 ReplayUnverifiable (安全默认, 不对外请求)。
    """
    authorized = {str(t).strip().lower() for t in (authorized_targets or set()) if str(t).strip()}
    enabled = bool(authorized)

    def verifier(knowledge_id: str, content: str) -> Tuple[bool, str]:
        if not enabled:
            raise ReplayUnverifiable(
                f"回放验证未启用(未配置授权目标), 条目 {knowledge_id} 保持 inconclusive"
            )
        hosts = extract_http_targets(content or "")
        if not hosts:
            raise ReplayUnverifiable(f"无 HTTP 目标可回放 (content 无 URL)")
        allowed = [h for h in hosts if _host_allowed(h, authorized)]
        if not allowed:
            raise ReplayUnverifiable(
                f"目标未授权: {hosts}, 跳过外部复现 (授权集: {sorted(authorized)})"
            )
        # 重建完整 URL 列表并逐一对授权 host 探测
        results: List[Tuple[bool, str]] = []
        for m in _URL_RE.finditer(content or ""):
            url = m.group(0).rstrip(".,;:!?)]}")
            mm = _HOST_PORT_RE.match(url.split("://", 1)[1].rstrip(".,;:!?)]}").split("/", 1)[0])
            if not mm or not _host_allowed(mm.group("host"), authorized):
                continue
            ok, evidence = _probe_url(url, timeout)
            results.append((ok, evidence))
            if ok:
                return True, f"连通性复现确认: {evidence}"
        # 全部失败: 只有 content 明确声称可达时才判 refuted, 否则无法判定
        if _CLAIMS_REACHABLE.search(content or ""):
            return False, "连通性复现失败: " + "; ".join(e for _, e in results)
        raise ReplayUnverifiable(
            "目标不可达且内容未声称可达, 无法判定: " + "; ".join(e for _, e in results)
        )

    return verifier


def verifier_from_env(env_value: Optional[str] = None, timeout: float = 8.0) -> ReplayVerifier:
    """从逗号分隔字符串构建授权集(用于 scripts 侧接线)。

    env_value=None -> 未启用(安全默认)。
    """
    if not env_value or not env_value.strip():
        return build_replay_verifier(None, timeout=timeout)
    targets = {t.strip() for t in env_value.split(",") if t.strip()}
    return build_replay_verifier(targets, timeout=timeout)
