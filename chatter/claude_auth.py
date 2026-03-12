"""Helpers for checking local Claude Code authentication state."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ClaudeAuthStatus:
    cli_available: bool
    logged_in: bool
    auth_method: str | None = None
    api_provider: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.cli_available and self.logged_in


def get_claude_auth_status(timeout_seconds: float = 5.0) -> ClaudeAuthStatus:
    """Return Claude Code auth state using `claude auth status --json`."""
    cli_path = shutil.which("claude")
    if not cli_path:
        return ClaudeAuthStatus(
            cli_available=False,
            logged_in=False,
            detail="Claude Code CLI not found in PATH.",
        )

    try:
        proc = subprocess.run(
            [cli_path, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ClaudeAuthStatus(
            cli_available=True,
            logged_in=False,
            detail="`claude auth status --json` timed out.",
        )
    except OSError as exc:
        return ClaudeAuthStatus(
            cli_available=True,
            logged_in=False,
            detail=f"Failed to run Claude Code CLI: {exc}",
        )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    payload: dict[str, Any] = {}
    if stdout:
        try:
            decoded = json.loads(stdout)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = {}

    auth_method = payload.get("authMethod")
    api_provider = payload.get("apiProvider")
    logged_in = bool(payload.get("loggedIn"))
    if logged_in:
        return ClaudeAuthStatus(
            cli_available=True,
            logged_in=True,
            auth_method=auth_method if isinstance(auth_method, str) else None,
            api_provider=api_provider if isinstance(api_provider, str) else None,
        )

    detail_parts: list[str] = []
    if stderr:
        detail_parts.append(stderr)
    if stdout and not payload:
        detail_parts.append(stdout)
    if not detail_parts and isinstance(auth_method, str):
        detail_parts.append(f"authMethod={auth_method}")
    if not detail_parts and proc.returncode:
        detail_parts.append(f"`claude auth status --json` exited with code {proc.returncode}.")

    return ClaudeAuthStatus(
        cli_available=True,
        logged_in=False,
        auth_method=auth_method if isinstance(auth_method, str) else None,
        api_provider=api_provider if isinstance(api_provider, str) else None,
        detail=" ".join(detail_parts) if detail_parts else None,
    )


def format_claude_auth_error(status: ClaudeAuthStatus) -> str:
    """Return a concise operator-facing remediation message."""
    lines = ["Claude Code login required."]
    if status.detail:
        lines.append(status.detail)
    if status.auth_method:
        lines.append(f"authMethod={status.auth_method}")
    lines.append("Run `claude auth login` in this shell environment, then retry.")
    lines.append("If the cached login looks stale, run `claude auth logout` first.")
    return "\n".join(lines)
