#!/usr/bin/env python3
"""Fail fast when repository hygiene drifts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
PACKAGE_INIT = REPO_ROOT / "chatter" / "__init__.py"
FORBIDDEN_ROOTS = {"build", "dist"}
FORBIDDEN_PARTS = {"__pycache__"}


def _fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _section_lines(text: str, header: str) -> list[str]:
    current_header = None
    lines: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_header = stripped
            continue
        if current_header == header:
            lines.append(raw_line)
    return lines


def _read_source_version() -> str:
    match = re.search(
        r'^__version__\s*=\s*"(?P<version>[^"]+)"\s*$',
        PACKAGE_INIT.read_text(),
        flags=re.MULTILINE,
    )
    if not match:
        raise RuntimeError(f"Could not find __version__ in {PACKAGE_INIT}")
    return match.group("version")


def _check_pyproject() -> int:
    text = PYPROJECT.read_text()
    project_lines = _section_lines(text, "[project]")
    dynamic_lines = _section_lines(text, "[tool.setuptools.dynamic]")

    if any(line.strip().startswith("version =") for line in project_lines):
        return _fail("pyproject.toml [project] should not define a hard-coded version.")

    dynamic_declared = any(
        re.search(r'dynamic\s*=\s*\[[^\]]*"version"[^\]]*\]', line)
        for line in project_lines
    )
    if not dynamic_declared:
        return _fail('pyproject.toml [project] must declare dynamic = ["version"].')

    if not any('version = {attr = "chatter.__version__"}' in line for line in dynamic_lines):
        return _fail(
            'pyproject.toml [tool.setuptools.dynamic] must use version = {attr = "chatter.__version__"}.'
        )

    return 0


def _check_version_source() -> int:
    version = _read_source_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return _fail(f'chatter.__version__ should look like "X.Y.Z", found "{version}".')
    return 0


def _check_tracked_artifacts() -> int:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    deleted_paths = {
        line[3:]
        for line in status.stdout.splitlines()
        if len(line) > 3 and "D" in line[:2]
    }
    offenders: list[str] = []
    for raw_path in tracked.stdout.splitlines():
        if raw_path in deleted_paths:
            continue
        path = Path(raw_path)
        if path.parts and path.parts[0] in FORBIDDEN_ROOTS:
            offenders.append(raw_path)
            continue
        if any(part in FORBIDDEN_PARTS for part in path.parts):
            offenders.append(raw_path)
            continue
        if any(part.endswith(".egg-info") for part in path.parts):
            offenders.append(raw_path)

    if offenders:
        print("ERROR: Generated artifacts are tracked in git:", file=sys.stderr)
        for path in offenders:
            print(f"  - {path}", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    checks = (
        _check_pyproject,
        _check_version_source,
        _check_tracked_artifacts,
    )
    for check in checks:
        status = check()
        if status:
            return status
    print("Repository guardrails passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())