"""Helpers for checking local Codex CLI authentication state."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CodexAuthStatus:
    cli_available: bool
    logged_in: bool
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.cli_available and self.logged_in


def get_codex_auth_status(timeout_seconds: float = 5.0) -> CodexAuthStatus:
    """Return Codex auth state using `codex login status`."""
    cli_path = shutil.which("codex")
    if not cli_path:
        return CodexAuthStatus(
            cli_available=False,
            logged_in=False,
            detail="Codex CLI not found in PATH.",
        )

    try:
        proc = subprocess.run(
            [cli_path, "login", "status"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CodexAuthStatus(
            cli_available=True,
            logged_in=False,
            detail="`codex login status` timed out.",
        )
    except OSError as exc:
        return CodexAuthStatus(
            cli_available=True,
            logged_in=False,
            detail=f"Failed to run Codex CLI: {exc}",
        )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    combined = "\n".join(part for part in (stdout, stderr) if part)
    lowered = combined.lower()
    if "logged in" in lowered and "not logged in" not in lowered:
        return CodexAuthStatus(
            cli_available=True,
            logged_in=True,
            detail=stdout or None,
        )

    detail_parts: list[str] = []
    if stdout:
        detail_parts.append(stdout)
    if stderr:
        detail_parts.append(stderr)
    if not detail_parts and proc.returncode:
        detail_parts.append(f"`codex login status` exited with code {proc.returncode}.")

    return CodexAuthStatus(
        cli_available=True,
        logged_in=False,
        detail=" ".join(detail_parts) if detail_parts else None,
    )


def format_codex_auth_error(status: CodexAuthStatus) -> str:
    """Return a concise operator-facing remediation message."""
    lines = ["Codex login required."]
    if status.detail:
        lines.append(status.detail)
    lines.append("Run `codex login` in this shell environment, then retry.")
    lines.append("For API-key auth, use `printenv OPENAI_API_KEY | codex login --with-api-key`.")
    return "\n".join(lines)
