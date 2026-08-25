#!/usr/bin/env python3
"""
secret_scan — 48 模式密钥/凭据扫描器 (Python 移植版)

从 Cybermes (Apache-2.0) 的 Go 实现 pkg/secrets/scanner.go 移植。
用途: APK/JS 逆向、源码审计、JS bundle 分析时的凭据狩猎。
覆盖: AWS/GCP/GitHub/Stripe/Slack/邮件服务/AI API (Anthropic/OpenAI/HF)/
      云基础设施/包注册表/SaaS/可观测性/隧道/机器人 token/私钥/JWT 等。

用法:
  python3 secret_scan.py <file_or_dir> [--workers 8] [--json]
  cat blob | python3 secret_scan.py -            # 从 stdin 读
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

PATTERNS = [
    # (name, severity, category, regex)
    # AWS
    ("AWS_ACCESS_KEY", "critical", "aws", r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("AWS_SECRET_TYPED", "critical", "aws", r"(?i)aws[_\-]?secret[_\-]?access[_\-]?key['\"\s:=]+([A-Za-z0-9/+=]{40})"),
    ("AWS_SECRET_LOOSE", "high", "aws", r"(?i)aws(.{0,20})?(secret|sk)['\"=: ]+([0-9a-z/+=]{40})"),
    # GCP
    ("GCP_SERVICE_ACCOUNT", "critical", "gcp", r'"type"\s*:\s*"service_account"'),
    ("GOOGLE_API_KEY", "high", "gcp", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # GitHub
    ("GH_PAT_CLASSIC", "critical", "github", r"\bghp_[A-Za-z0-9]{36}\b"),
    ("GH_PAT_FINEGRAINED", "critical", "github", r"\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    ("GH_OAUTH", "high", "github", r"\bgho_[A-Za-z0-9]{36}\b"),
    ("GH_S2S", "high", "github", r"\bgh[usr]_[A-Za-z0-9]{36,}\b"),
    # Stripe
    ("STRIPE_LIVE", "critical", "stripe", r"\bsk_live_[0-9A-Za-z]{24,}\b"),
    ("STRIPE_TEST", "low", "stripe", r"\bsk_test_[0-9A-Za-z]{24,}\b"),
    # Slack
    ("SLACK_TOKEN", "high", "slack", r"\bxox[abpors]-[0-9A-Za-z\-]{10,48}\b"),
    ("SLACK_WEBHOOK", "medium", "slack", r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
    # 邮件服务
    ("SENDGRID", "high", "email_svc", r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b"),
    ("MAILGUN_V1", "high", "email_svc", r"\bkey-[0-9a-zA-Z]{32}\b"),
    ("MAILGUN_LOOSE", "high", "email_svc", r"\bkey-[0-9a-f]{32}\b"),
    # Twilio
    ("TWILIO_API", "high", "twilio", r"\bSK[0-9a-fA-F]{32}\b"),
    ("TWILIO_SID", "medium", "twilio", r"\bAC[a-f0-9]{32}\b"),
    ("TWILIO_AUTH", "high", "twilio", r"(?i)twilio(.{0,20})?(auth|token)['\"=: ]+([a-f0-9]{32})"),
    # PaaS
    ("HEROKU_API", "medium", "paas", r"(?i)heroku(.{0,20})?api['\"=: ]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
    # Firebase
    ("FIREBASE_URL", "low", "firebase", r"\bhttps?://[a-z0-9\-]+\.firebaseio\.com\b"),
    # Token / auth headers
    ("JWT", "medium", "jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    ("BEARER_AUTH", "medium", "bearer", r"(?i)authorization['\"=: ]+bearer\s+[A-Za-z0-9._\-]{20,}"),
    ("BASIC_AUTH_URL", "medium", "basic_auth", r"https?://[^/\s:@]+:[^/\s:@]+@[^/\s]+"),
    # 私钥
    ("RSA_PRIVKEY", "critical", "private_key", r"-----BEGIN RSA PRIVATE KEY-----"),
    ("EC_PRIVKEY", "critical", "private_key", r"-----BEGIN EC PRIVATE KEY-----"),
    ("OPENSSH_PRIVKEY", "critical", "private_key", r"-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("GENERIC_PRIVKEY", "critical", "private_key", r"-----BEGIN (DSA |PGP |)PRIVATE KEY-----"),
    # 通用
    ("GENERIC_API_KEY", "medium", "generic", r"(?i)(?:api[_\-]?key|apikey|api_secret|access_token|secret[_\-]?token)['\"\s:=]+[\"']([A-Za-z0-9+/=_\-]{24,})[\"']"),
    # 现代 AI API
    ("ANTHROPIC_API", "critical", "ai_api", r"\bsk-ant-(?:api03|admin01)-[A-Za-z0-9_\-]{93,}\b"),
    ("OPENAI_LEGACY", "critical", "ai_api", r"\bsk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}\b"),
    ("OPENAI_PROJECT", "critical", "ai_api", r"\bsk-proj-[A-Za-z0-9_\-]{40,}T3BlbkFJ[A-Za-z0-9_\-]{40,}\b"),
    ("OPENAI_SESSION", "high", "ai_api", r"\bsess-[A-Za-z0-9]{40}\b"),
    ("HUGGINGFACE", "high", "ai_api", r"\bhf_[A-Za-z0-9]{30,}\b"),
    # 云基础设施
    ("CLOUDFLARE_API", "critical", "infra_api", r"(?i)cf[_\-]?api[_\-]?key['\"\s:=]+([a-f0-9]{37})"),
    ("DIGITALOCEAN", "high", "infra_api", r"\bdop_v1_[a-f0-9]{64}\b"),
    # 包注册表
    ("NPM_TOKEN", "high", "package_registry", r"\bnpm_[A-Za-z0-9]{36}\b"),
    ("PYPI_TOKEN", "high", "package_registry", r"\bpypi-AgENdGV[A-Za-z0-9_\-]+\b"),
    ("DOCKER_HUB_PAT", "high", "package_registry", r"\bdckr_pat_[A-Za-z0-9_\-]{27,}\b"),
    # SaaS
    ("ATLASSIAN_TOKEN", "high", "saas_api", r"\bATATT3xFfGF0[A-Za-z0-9_\-]{180,}\b"),
    ("LINEAR_API", "medium", "saas_api", r"\blin_api_[A-Za-z0-9]{40}\b"),
    # 可观测性
    ("NEWRELIC_LICENSE", "medium", "observability", r"\b(?:NRAA|NRAK|NRBR)-[A-F0-9]{27}\b"),
    ("DATADOG_API", "high", "observability", r"(?i)dd[_\-]?api[_\-]?key['\"\s:=]+([a-f0-9]{32})"),
    ("SENTRY_DSN", "low", "observability", r"https://\w+@o[0-9]+\.ingest\.sentry\.io/[0-9]+"),
    # 隧道
    ("NGROK_AUTH", "medium", "tunneling", r"\b[12][A-Za-z0-9]{26}_[A-Za-z0-9]{32,}\b"),
    # 机器人 token
    ("DISCORD_BOT", "high", "bot_token", r"\b[MN][A-Za-z\d]{23}\.[\w\-]{6}\.[\w\-]{27}\b"),
    ("TELEGRAM_BOT", "high", "bot_token", r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),
]

COMPILED = [(name, sev, cat, re.compile(expr)) for name, sev, cat, expr in PATTERNS]

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build", ".tox", ".mypy_cache"}
SKIP_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".woff", ".woff2",
             ".ttf", ".eot", ".otf", ".mp4", ".mp3", ".webm", ".avi", ".mov", ".zip",
             ".gz", ".tar", ".7z", ".pdf", ".class", ".pyc", ".so", ".dll", ".exe", ".jar"}
MAX_FILE = 20 * 1024 * 1024  # 20MB 上限


def scan_line(line: str, line_no: int, source: str):
    findings = []
    for name, sev, cat, re_obj in COMPILED:
        for m in re_obj.finditer(line):
            match_text = m.group(0)
            if m.lastindex and m.group(1):
                match_text = m.group(1)
            findings.append({
                "pattern": name, "severity": sev, "category": cat,
                "match": match_text[:80], "source": source, "line": line_no,
            })
    return findings


def scan_text(text: str, source: str = "<stdin>"):
    findings = []
    for i, line in enumerate(text.splitlines(), 1):
        findings.extend(scan_line(line, i, source))
    return findings


def scan_file(path: str):
    try:
        if os.path.getsize(path) > MAX_FILE:
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return scan_text(f.read(), path)
    except (OSError, UnicodeDecodeError):
        return []


def scan_dir(path: str, workers: int = 8):
    files = []
    for dp, dn, fn in os.walk(path):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for name in fn:
            if os.path.splitext(name)[1].lower() in SKIP_EXTS:
                continue
            files.append(os.path.join(dp, name))
    all_findings = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(scan_file, files):
            all_findings.extend(res)
    return all_findings


def main():
    ap = argparse.ArgumentParser(description="48-pattern secret/credential scanner (Python port)")
    ap.add_argument("target", nargs="?", default="-", help="File, dir, or '-' for stdin")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", action="store_true", help="Output as JSON")
    args = ap.parse_args()

    if args.target == "-":
        findings = scan_text(sys.stdin.read())
    elif os.path.isdir(args.target):
        findings = scan_dir(args.target, args.workers)
    elif os.path.isfile(args.target):
        findings = scan_file(args.target)
    else:
        print(f"error: {args.target} not found", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        findings.sort(key=lambda f: sev_order.get(f["severity"], 9))
        print(f"🔍 secret_scan: {len(findings)} finding(s)\n")
        for f in findings:
            print(f"[{f['severity'].upper():8s}] {f['pattern']:22s} {f['source']}:{f['line']}")
            print(f"         {f['match']}")
    sys.exit(0)


if __name__ == "__main__":
    main()
