#!/usr/bin/env python3
"""PostToolUse hook: format and lint the Python file Claude just edited.

Reads the hook payload from stdin, runs `ruff check --fix` then `ruff format`
on the edited file, and exits 2 with the remaining lint errors on stderr so
Claude sees them and fixes them. Never fails the session for tooling reasons:
a missing uv or ruff, a timeout or a non-Python file all exit 0.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "__pycache__"}
TIMEOUT_S = 30


def ruff_cmd() -> list[str] | None:
    if shutil.which("uv"):
        return ["uv", "run", "--quiet", "ruff"]
    if shutil.which("ruff"):
        return ["ruff"]
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return 0

    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    path = Path(file_path)
    if not path.is_absolute():
        path = project / path
    path = path.resolve()

    if path.suffix not in {".py", ".pyi"} or not path.is_file():
        return 0
    if project not in path.parents:
        return 0
    if SKIP_DIRS & set(path.relative_to(project).parts):
        return 0

    ruff = ruff_cmd()
    if ruff is None:
        return 0

    try:
        check = subprocess.run(
            [*ruff, "check", "--fix", "--quiet", str(path)],
            cwd=project, capture_output=True, text=True, timeout=TIMEOUT_S,
        )
        subprocess.run(
            [*ruff, "format", "--quiet", str(path)],
            cwd=project, capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0

    if check.returncode == 1:  # ruff: violations remain after --fix
        rel = path.relative_to(project)
        print(f"ruff found issues it could not auto-fix in {rel}:", file=sys.stderr)
        print((check.stdout or check.stderr).strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
