"""Acceptance: no API keys, tokens or passwords are committed to the repository."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                                  # AWS access key
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),                           # common API key format
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),                       # GitHub tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),                       # Slack tokens
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),                     # Telegram bot token
    re.compile(r"(?i)(client_secret|api_key|apikey|password|passwd|secret)\s*[=:]\s*['\"][^'\"\s{}<>]{8,}['\"]"),
    re.compile(r"(?i)(EBAY_CLIENT_SECRET|WEBHOOK_SECRET)\s*=\s*[A-Za-z0-9]{8,}"),
]
ALLOW = ("tests/test_no_secrets.py",)


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / f for f in out.splitlines() if f]
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")


def test_no_secrets_in_repository():
    hits = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOW or not path.is_file() or path.suffix in (".png", ".jpg", ".db"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for rx in PATTERNS:
            for m in rx.finditer(text):
                hits.append(f"{rel}: {m.group(0)[:40]}")
    assert not hits, "possible secrets committed:\n" + "\n".join(hits)


def test_local_config_files_are_gitignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for name in ("affiliate_config.json", "sources.json", "automation.json", ".env", "data/"):
        assert name in ignore, f"{name} should be in .gitignore"
