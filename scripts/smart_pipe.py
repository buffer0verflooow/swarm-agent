#!/usr/bin/env python3
"""
smart_pipe — 高信号输出流过滤器 (Python 移植版)

从 Cybermes (Apache-2.0) 的 Go 实现 pkg/stream/filter.go 移植。
用途: 蜂群 worker 消费 katana/ffuf/nuclei/httpx 等工具输出时,
     全量原始日志归档到 recon/<SLUG>/<tool>_raw.txt, 仅把 top-N
     高信号行注入 agent 上下文, 节省 70-85% token。

用法:
  <tool_cmd> | python3 smart_pipe.py --target <SLUG> --tool <NAME> [--limit 40]
  cat file | python3 smart_pipe.py -t SLUG -n TOOL -l 20

启发式打分 (与 Go 版一致):
  静态资源后缀           -> 0 分 (丢弃)
  关键标记 (cve/rce/idor 等) -> +80
  密钥标记 (.env/.git/jwt 等) -> +60
  HTTP 200 + /api/        -> +25/+25
  401/403 + /admin|api    -> +20/+25
  500                     -> +15
  查询参数 (?a=b)          -> +20
  UUID                    -> +20
  key/secret/tok/pass + 熵>3.8 -> +30
"""
import argparse
import math
import re
import sys
import os
import uuid as _uuid_mod

ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
UUID_RE = re.compile(r'(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

STATIC_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".css", ".mp4", ".mp3", ".webm", ".avi", ".mov",
)
CRITICAL_MARKERS = (
    "[critical]", "[high]", "cve-", "rce", "sql injection",
    "sqli", "idor", "ssrf", "xxe", "auth bypass",
)
SECRET_MARKERS = (
    ".env", ".git", "swagger", "openapi", "graphql",
    "id_rsa", "password", "secret_key", "bearer ", "token=", "jwt",
)


def clean_line(line: str) -> str:
    return ANSI_RE.sub("", line).strip()


def calculate_entropy(text: str) -> float:
    if len(text) < 16:
        return 0.0
    counts = [0] * 256
    for b in text.encode('utf-8', errors='ignore'):
        counts[b] += 1
    n = float(len(text))
    entropy = 0.0
    for c in counts:
        if c > 0:
            p = c / n
            entropy -= p * math.log2(p)
    return entropy


def is_static_asset(lower: str) -> bool:
    for ext in STATIC_SUFFIXES:
        if lower.endswith(ext) or ext + "?" in lower or ext + "#" in lower:
            return True
    return False


def score_line(line: str) -> int:
    lower = line.lower()
    if is_static_asset(lower):
        return 0
    score = 10
    if any(m in lower for m in CRITICAL_MARKERS):
        score += 80
    if any(m in lower for m in SECRET_MARKERS):
        score += 60
    if "200 ok" in lower or "[200]" in lower:
        score += 25
        if "/api/" in lower or "/v1/" in lower or "/v2/" in lower:
            score += 25
    elif any(x in lower for x in ("[401]", "[403]", "401 unauthorized", "403 forbidden")):
        score += 20
        if any(x in lower for x in ("/admin", "/api/", "/internal")):
            score += 25
    elif any(x in lower for x in ("[500]", "500 internal server error")):
        score += 15
    if "?" in line and "=" in line:
        score += 20
    if UUID_RE.search(line):
        score += 20
    if any(k in lower for k in ("key", "secret", "tok", "pass")):
        if calculate_entropy(line) > 3.8:
            score += 30
    return score


def process_stream(stdin, stdout, raw_out, limit: int) -> dict:
    total_raw = 0
    scored = []
    seen = set()
    for line in stdin:
        cleaned = clean_line(line)
        if not cleaned:
            continue
        total_raw += 1
        raw_out.write(cleaned + "\n")
        if cleaned not in seen:
            seen.add(cleaned)
            s = score_line(cleaned)
            if s > 0:
                scored.append((s, cleaned))
    scored.sort(key=lambda x: x[0], reverse=True)
    display = scored[:limit]
    stdout.write(f"📊 [Smart Filter] {len(display)} high-signal findings prioritized (from {total_raw} total raw lines).\n\n")
    for s, text in display:
        stdout.write(text + "\n")
    if len(scored) > len(display):
        stdout.write(f"\n... (+{len(scored) - len(display)} more filtered entries archived in raw log)\n")
    return {
        "total_raw": total_raw,
        "unique_scored": len(scored),
        "shown": len(display),
        "preserved": len(scored) - len(display),
    }


def find_project_root() -> str:
    """从当前目录向上找含 AGENTS.md 的目录 (与 Go 版一致)"""
    cwd = os.getcwd()
    d = cwd
    while True:
        if os.path.exists(os.path.join(d, "AGENTS.md")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return cwd


def main():
    ap = argparse.ArgumentParser(description="High-signal stream filter (smart_pipe Python port)")
    ap.add_argument("--target", "-t", default="default_target", help="Target slug identifier")
    ap.add_argument("--tool", "-n", default="tool", help="Security tool name (katana, ffuf, httpx...)")
    ap.add_argument("--limit", "-l", type=int, default=40, help="Max prioritized lines to display")
    ap.add_argument("--raw-dir", default=None, help="Override raw archive dir (default: <root>/recon/<SLUG>)")
    args = ap.parse_args()

    if sys.stdin.isatty():
        print("Usage: <tool_command> | python3 smart_pipe.py --target <SLUG> --tool <TOOL>", file=sys.stderr)
        sys.exit(1)

    if args.raw_dir:
        recon_dir = args.raw_dir
    else:
        recon_dir = os.path.join(find_project_root(), "recon", args.target)
    os.makedirs(recon_dir, exist_ok=True)
    raw_path = os.path.join(recon_dir, f"{args.tool}_raw.txt")
    with open(raw_path, "w", encoding="utf-8", errors="replace") as raw_file:
        res = process_stream(sys.stdin, sys.stdout, raw_file, args.limit)
    print(f"💾 Full raw output preserved: {raw_path}")
    return res


if __name__ == "__main__":
    main()
