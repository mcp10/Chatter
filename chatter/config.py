"""Configuration management for Chatter.

Two config files:
  ~/.chatter/config.yaml   — global, stores allowed_user_id (written once)
  .chatter.yaml            — per-repo, stores bot_token and repo_name (gitignored)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

GLOBAL_CONFIG_DIR = Path.home() / ".chatter"
GLOBAL_CONFIG_FILE = GLOBAL_CONFIG_DIR / "config.yaml"
REPO_CONFIG_NAME = ".chatter.yaml"


@dataclass
class GlobalConfig:
    allowed_user_id: int

    @classmethod
    def load(cls) -> "GlobalConfig":
        try:
            data = yaml.safe_load(GLOBAL_CONFIG_FILE.read_text())
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Global config not found at {GLOBAL_CONFIG_FILE}. Run `chatter init` first."
            )
        return cls(allowed_user_id=int(data["allowed_user_id"]))

    @classmethod
    def save(cls, allowed_user_id: int) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        GLOBAL_CONFIG_FILE.write_text(yaml.dump({"allowed_user_id": allowed_user_id}))


@dataclass
class RepoConfig:
    bot_token: str
    repo_name: str

    @classmethod
    def load(cls, path: Path | None = None) -> "RepoConfig":
        config_path = path or (Path.cwd() / REPO_CONFIG_NAME)
        try:
            data = yaml.safe_load(config_path.read_text())
        except FileNotFoundError:
            raise FileNotFoundError(
                f"No {REPO_CONFIG_NAME} found in {config_path.parent}. Run `chatter init` first."
            )
        return cls(bot_token=data["bot_token"], repo_name=data.get("repo_name", config_path.parent.name))

    @classmethod
    def save(cls, path: Path, bot_token: str, repo_name: str) -> None:
        path.write_text(yaml.dump({"bot_token": bot_token, "repo_name": repo_name}))
