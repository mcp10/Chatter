"""Tests for configuration loading, saving, and validation."""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from chatter.config import (
    ChatterConfig,
    RepoEntry,
    _check_permissions,
    _secure_path,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_config(path: Path, data: dict) -> None:
    """Write a YAML config file with secure permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False))
    os.chmod(path, 0o600)


VALID_CONFIG = {
    "allowed_user_id": 12345678,
    "repos": {
        "myrepo": {
            "bot_token": "123456:ABC-test-token",
            "path": "/home/user/myrepo",
            "agent_backend": "claude",
        }
    },
}


# ---------------------------------------------------------------------------
# ChatterConfig.load
# ---------------------------------------------------------------------------

class TestConfigLoad:
    def test_load_valid_config(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        write_config(config_file, VALID_CONFIG)

        with patch("chatter.config.GLOBAL_CONFIG_FILE", config_file):
            cfg = ChatterConfig.load()
            assert cfg.allowed_user_id == 12345678
            assert "myrepo" in cfg.repos
            assert cfg.repos["myrepo"].bot_token == "123456:ABC-test-token"
            assert cfg.repos["myrepo"].path == "/home/user/myrepo"

    def test_load_missing_file_raises(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.yaml"
        with patch("chatter.config.GLOBAL_CONFIG_FILE", missing), pytest.raises(FileNotFoundError, match="chatter init"):
            ChatterConfig.load()

    def test_load_empty_file_raises(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        os.chmod(config_file, 0o600)
        with patch("chatter.config.GLOBAL_CONFIG_FILE", config_file), pytest.raises((KeyError, TypeError)):
            ChatterConfig.load()

    def test_load_no_repos_key(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        write_config(config_file, {"allowed_user_id": 111})
        with patch("chatter.config.GLOBAL_CONFIG_FILE", config_file):
            cfg = ChatterConfig.load()
            assert cfg.allowed_user_id == 111
            assert cfg.repos == {}

    def test_load_multiple_repos(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        data = {
            "allowed_user_id": 111,
            "repos": {
                "repo1": {"bot_token": "tok1", "path": "/p1"},
                "repo2": {"bot_token": "tok2", "path": "/p2", "agent_backend": "codex"},
            },
        }
        write_config(config_file, data)
        with patch("chatter.config.GLOBAL_CONFIG_FILE", config_file):
            cfg = ChatterConfig.load()
            assert len(cfg.repos) == 2
            assert cfg.repos["repo2"].agent_backend == "codex"


# ---------------------------------------------------------------------------
# ChatterConfig.save
# ---------------------------------------------------------------------------

class TestConfigSave:
    def test_save_creates_file(self, tmp_path: Path):
        config_dir = tmp_path / ".chatter"
        config_file = config_dir / "config.yaml"

        with (
            patch("chatter.config.GLOBAL_CONFIG_DIR", config_dir),
            patch("chatter.config.GLOBAL_CONFIG_FILE", config_file),
        ):
            cfg = ChatterConfig(
                allowed_user_id=42,
                repos={"r": RepoEntry(bot_token="t", path="/p")},
            )
            cfg.save()

            assert config_file.exists()
            data = yaml.safe_load(config_file.read_text())
            assert data["allowed_user_id"] == 42
            assert data["repos"]["r"]["bot_token"] == "t"

    def test_save_roundtrip(self, tmp_path: Path):
        config_dir = tmp_path / ".chatter"
        config_file = config_dir / "config.yaml"

        with (
            patch("chatter.config.GLOBAL_CONFIG_DIR", config_dir),
            patch("chatter.config.GLOBAL_CONFIG_FILE", config_file),
        ):
            original = ChatterConfig(
                allowed_user_id=99,
                repos={
                    "a": RepoEntry(bot_token="ta", path="/pa"),
                    "b": RepoEntry(bot_token="tb", path="/pb", agent_backend="codex"),
                },
            )
            original.save()

            loaded = ChatterConfig.load()
            assert loaded.allowed_user_id == original.allowed_user_id
            assert loaded.repos["a"].bot_token == "ta"
            assert loaded.repos["b"].agent_backend == "codex"


# ---------------------------------------------------------------------------
# ChatterConfig.find_repo_by_cwd
# ---------------------------------------------------------------------------

class TestFindRepoByCwd:
    def test_finds_matching_repo(self, tmp_path: Path):
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        cfg = ChatterConfig(
            allowed_user_id=1,
            repos={"myrepo": RepoEntry(bot_token="t", path=str(repo_path))},
        )

        with patch("pathlib.Path.cwd", return_value=repo_path):
            name, entry = cfg.find_repo_by_cwd()
            assert name == "myrepo"
            assert entry.bot_token == "t"

    def test_raises_on_no_match(self, tmp_path: Path):
        cfg = ChatterConfig(
            allowed_user_id=1,
            repos={"other": RepoEntry(bot_token="t", path="/some/other/path")},
        )
        with patch("pathlib.Path.cwd", return_value=tmp_path), pytest.raises(LookupError, match="chatter init"):
            cfg.find_repo_by_cwd()


# ---------------------------------------------------------------------------
# ChatterConfig.add_repo
# ---------------------------------------------------------------------------

class TestAddRepo:
    def test_add_repo_saves(self, tmp_path: Path):
        config_dir = tmp_path / ".chatter"
        config_file = config_dir / "config.yaml"

        with (
            patch("chatter.config.GLOBAL_CONFIG_DIR", config_dir),
            patch("chatter.config.GLOBAL_CONFIG_FILE", config_file),
        ):
            cfg = ChatterConfig(allowed_user_id=1)
            cfg.add_repo("new-repo", "token123", "/repo/path")

            assert "new-repo" in cfg.repos
            assert cfg.repos["new-repo"].bot_token == "token123"
            # Verify it was persisted
            assert config_file.exists()

    def test_add_repo_resolves_path(self, tmp_path: Path):
        config_dir = tmp_path / ".chatter"
        config_file = config_dir / "config.yaml"
        real_dir = tmp_path / "real-repo"
        real_dir.mkdir()

        with (
            patch("chatter.config.GLOBAL_CONFIG_DIR", config_dir),
            patch("chatter.config.GLOBAL_CONFIG_FILE", config_file),
        ):
            cfg = ChatterConfig(allowed_user_id=1)
            cfg.add_repo("r", "t", str(real_dir))
            # Path should be resolved (absolute)
            assert Path(cfg.repos["r"].path).is_absolute()


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------

class TestPermissions:
    def test_save_sets_config_file_permissions(self, tmp_path: Path):
        config_dir = tmp_path / ".chatter"
        config_file = config_dir / "config.yaml"

        with (
            patch("chatter.config.GLOBAL_CONFIG_DIR", config_dir),
            patch("chatter.config.GLOBAL_CONFIG_FILE", config_file),
        ):
            cfg = ChatterConfig(allowed_user_id=1)
            cfg.save()

            file_mode = config_file.stat().st_mode & 0o777
            assert file_mode == 0o600

    def test_save_sets_config_dir_permissions(self, tmp_path: Path):
        config_dir = tmp_path / ".chatter"
        config_file = config_dir / "config.yaml"

        with (
            patch("chatter.config.GLOBAL_CONFIG_DIR", config_dir),
            patch("chatter.config.GLOBAL_CONFIG_FILE", config_file),
        ):
            cfg = ChatterConfig(allowed_user_id=1)
            cfg.save()

            dir_mode = config_dir.stat().st_mode & 0o777
            assert dir_mode == 0o700

    def test_check_permissions_warns_on_world_readable(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("test")
        os.chmod(config_file, 0o644)  # group+other readable

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _check_permissions(config_file)
            assert len(w) == 1
            assert "overly permissive" in str(w[0].message)

    def test_check_permissions_silent_on_secure(self, tmp_path: Path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("test")
        os.chmod(config_file, 0o600)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _check_permissions(config_file)
            assert len(w) == 0

    def test_secure_path_helper(self, tmp_path: Path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("test")
        os.chmod(test_file, 0o644)

        _secure_path(test_file, 0o600)
        assert (test_file.stat().st_mode & 0o777) == 0o600
