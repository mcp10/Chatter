"""Configuration management for Chatter.

Single config file at ~/.chatter/config.yaml stores everything:
  allowed_user_id  — Telegram user ID
  repos            — map of repo name → {bot_token, path}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

GLOBAL_CONFIG_DIR = Path.home() / ".chatter"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.yaml"


@dataclass
class RepoEntry:
    bot_token: str
    path: str


@dataclass
class ChatterConfig:
    allowed_user_id: int
    repos: dict[str, RepoEntry] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "ChatterConfig":
        try:
            data = yaml.safe_load(GLOBAL_CONFIG_FILE.read_text())
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Config not found at {GLOBAL_CONFIG_FILE}. Run `chatter init` first."
            )
        repos = {}
        for name, entry in (data.get("repos") or {}).items():
            repos[name] = RepoEntry(bot_token=entry["bot_token"], path=entry["path"])
        return cls(
            allowed_user_id=int(data["allowed_user_id"]),
            repos=repos,
        )

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "allowed_user_id": self.allowed_user_id,
            "repos": {
                name: {"bot_token": entry.bot_token, "path": entry.path}
                for name, entry in self.repos.items()
            },
        }
        GLOBAL_CONFIG_FILE.write_text(yaml.dump(data, default_flow_style=False))

    def find_repo_by_cwd(self) -> tuple[str, RepoEntry]:
        cwd = str(Path.cwd().resolve())
        for name, entry in self.repos.items():
            if str(Path(entry.path).resolve()) == cwd:
                return name, entry
        raise LookupError(
            f"No repo registered for {cwd}. Run `chatter init` in this directory first."
        )

    def add_repo(self, name: str, bot_token: str, path: str) -> None:
        self.repos[name] = RepoEntry(bot_token=bot_token, path=str(Path(path).resolve()))
        self.save()
