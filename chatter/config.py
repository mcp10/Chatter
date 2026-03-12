"""Configuration management for Chatter.

Single config file at ~/.chatter/config.yaml stores everything:
  allowed_user_id  — Telegram user ID
  repos            — map of repo name → {bot_token, path}
"""

from __future__ import annotations

import os
import stat
import warnings
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .agent import DEFAULT_AGENT_BACKEND, normalize_agent_backend

GLOBAL_CONFIG_DIR = Path.home() / ".chatter"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.yaml"
GLOBAL_LOG_DIR = GLOBAL_CONFIG_DIR / "logs"

# Permission bits that indicate group/other access
_TOO_OPEN = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH


def _secure_path(path: Path, mode: int) -> None:
    """Set file/directory permissions, ignoring errors on non-Unix systems."""
    with suppress(OSError):
        os.chmod(path, mode)


def _check_permissions(path: Path) -> None:
    """Warn if a file is readable by group or others."""
    try:
        file_mode = path.stat().st_mode
        if file_mode & _TOO_OPEN:
            warnings.warn(
                f"Config file {path} has overly permissive permissions "
                f"(mode {oct(file_mode & 0o777)}). "
                f"Run: chmod 600 {path}",
                stacklevel=3,
            )
    except OSError:
        pass


@dataclass
class RepoEntry:
    bot_token: str
    path: str
    agent_backend: str = DEFAULT_AGENT_BACKEND


@dataclass
class ChatterConfig:
    allowed_user_id: int
    repos: dict[str, RepoEntry] = field(default_factory=dict)

    @classmethod
    def load(cls) -> ChatterConfig:
        try:
            data = yaml.safe_load(GLOBAL_CONFIG_FILE.read_text())
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Config not found at {GLOBAL_CONFIG_FILE}. Run `chatter init` first."
            ) from exc

        # Warn if config file has overly permissive permissions (Unix only)
        _check_permissions(GLOBAL_CONFIG_FILE)

        if data is None:
            data = {}
        repos = {}
        for name, entry in (data.get("repos") or {}).items():
            repos[name] = RepoEntry(
                bot_token=entry["bot_token"],
                path=entry["path"],
                agent_backend=normalize_agent_backend(entry.get("agent_backend")),
            )
        return cls(
            allowed_user_id=int(data["allowed_user_id"]),
            repos=repos,
        )

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _secure_path(GLOBAL_CONFIG_DIR, 0o700)
        data = {
            "allowed_user_id": self.allowed_user_id,
            "repos": {
                name: {
                    "bot_token": entry.bot_token,
                    "path": entry.path,
                    "agent_backend": normalize_agent_backend(entry.agent_backend),
                }
                for name, entry in self.repos.items()
            },
        }
        GLOBAL_CONFIG_FILE.write_text(yaml.dump(data, default_flow_style=False))
        _secure_path(GLOBAL_CONFIG_FILE, 0o600)

    def find_repo_by_cwd(self) -> tuple[str, RepoEntry]:
        cwd = os.path.normcase(str(Path.cwd().resolve()))
        for name, entry in self.repos.items():
            if os.path.normcase(str(Path(entry.path).resolve())) == cwd:
                return name, entry
        raise LookupError(
            f"No repo registered for {cwd}. Run `chatter init` in this directory first."
        )

    def add_repo(
        self,
        name: str,
        bot_token: str,
        path: str,
        agent_backend: str = DEFAULT_AGENT_BACKEND,
    ) -> None:
        self.repos[name] = RepoEntry(
            bot_token=bot_token,
            path=str(Path(path).resolve()),
            agent_backend=normalize_agent_backend(agent_backend),
        )
        self.save()
