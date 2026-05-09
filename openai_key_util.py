"""Resolve OPENAI_API_KEY from the environment or a repo-local .env file.

Convention: never commit API keys. Use one of:
  - Environment variable OPENAI_API_KEY (recommended for CI and shells)
  - A file named .env in the repo root with a line: OPENAI_API_KEY=sk-...
    (.env is gitignored)
"""
from __future__ import annotations

import os
from pathlib import Path


def _normalize_api_key(raw: str) -> str:
    """Strip whitespace, BOM artifacts, CRLF, and matching outer quotes (common .env mistakes)."""
    s = raw.replace("\r", "").strip()
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff")
    prev = None
    while prev != s:
        prev = s
        s = s.strip()
        if len(s) >= 2:
            q1, q2 = s[0], s[-1]
            if q1 == q2 and q1 in ('"', "'"):
                s = s[1:-1]
    return s.strip()


def load_openai_api_key(*, base_dir: Path) -> str:
    env_raw = os.getenv("OPENAI_API_KEY")
    key = _normalize_api_key(env_raw) if env_raw else ""
    if key:
        return key
    env_path = base_dir / ".env"
    if not env_path.is_file():
        return ""
    try:
        # utf-8-sig: skip UTF-8 BOM if Notepad wrote one
        text = env_path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.replace("\r", "").strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() != "OPENAI_API_KEY":
            continue
        return _normalize_api_key(value)
    return ""
