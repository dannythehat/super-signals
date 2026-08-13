#!/usr/bin/env python3
"""Fail a build when source contains a high-confidence credential pattern.

Only file paths and rule names are printed. Secret values are never echoed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".zip",
    ".gz",
    ".pdf",
    ".woff",
    ".woff2",
}
MAX_BYTES = 2_000_000

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "credentialed_database_url",
        re.compile(r"\bpostgres(?:ql)?://[^/\s:@]+:[^@\s/]+@[^/\s]+", re.IGNORECASE),
    ),
)

# Deliberately public/local examples that are allowed in source-controlled templates
# and tests. Keep this list exact; never exempt a whole file or directory merely
# because a credential-shaped string is inconvenient to scan.
ALLOWED_SUBSTRINGS = (
    "postgresql+psycopg://super_signals:super_signals@127.0.0.1:5432/super_signals",
    "postgresql://super_signals:super_signals@127.0.0.1:5432/super_signals",
    "postgresql://user:password@db.example.com/app",
)


def _iter_text_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        yield path, text


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings: list[tuple[str, str]] = []
    for path, text in _iter_text_files(root):
        scan_text = text
        for allowed in ALLOWED_SUBSTRINGS:
            scan_text = scan_text.replace(allowed, "")
        for rule_name, pattern in RULES:
            if pattern.search(scan_text):
                findings.append((str(path.relative_to(root)), rule_name))

    if findings:
        print("Day 39 secret scan FAILED. Credential-like material found:")
        for file_path, rule_name in findings:
            print(f"- {file_path}: {rule_name}")
        print("Matched values are intentionally not printed.")
        return 1

    print("Day 39 secret scan passed: no high-confidence credential patterns found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
