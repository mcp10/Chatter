"""Shared test fixtures for Chatter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake repo root fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Create a temporary directory that acts as the repo root."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    # Create some subdirectories/files to simulate a repo
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("# main")
    (repo / "README.md").write_text("# Readme")
    # Resolve to follow symlinks (macOS /private/var/... vs /var/...)
    # This matches production behavior where _repo_root() calls .resolve()
    return repo.resolve()


# ---------------------------------------------------------------------------
# Mock BotConfig
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_bot_config(repo_root: Path):
    """Return a BotConfig-like object for testing."""
    from chatter.bot import BotConfig
    return BotConfig(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        allowed_user_id=12345678,
        repo_path=str(repo_root),
        repo_name="test-repo",
        agent_backend="claude",
    )


# ---------------------------------------------------------------------------
# Mock Telegram Update objects
# ---------------------------------------------------------------------------

def make_update(
    user_id: int = 12345678,
    chat_type: str = "private",
    chat_id: int = 99999,
) -> MagicMock:
    """Create a mock Telegram Update with configurable user/chat."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    return update


@pytest.fixture
def allowed_update() -> MagicMock:
    """Update from the allowed user in a private chat."""
    return make_update(user_id=12345678, chat_type="private")


@pytest.fixture
def wrong_user_update() -> MagicMock:
    """Update from a different user."""
    return make_update(user_id=99999999, chat_type="private")


@pytest.fixture
def group_update() -> MagicMock:
    """Update from the allowed user but in a group chat."""
    return make_update(user_id=12345678, chat_type="group")
