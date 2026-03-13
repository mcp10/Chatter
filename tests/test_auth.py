"""Tests for authentication and authorization checks."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from chatter import bot
from tests.conftest import make_update


class TestIsAllowed:
    """Test the is_allowed() gatekeeper."""

    @pytest.fixture(autouse=True)
    def _setup_config(self, mock_bot_config):
        """Inject mock config for auth tests."""
        original = bot._config
        bot._config = mock_bot_config
        yield
        bot._config = original

    def test_allowed_user_private_chat(self, allowed_update):
        assert bot.is_allowed(allowed_update)

    def test_wrong_user_denied(self, wrong_user_update):
        assert not bot.is_allowed(wrong_user_update)

    def test_group_chat_denied(self, group_update):
        assert not bot.is_allowed(group_update)

    def test_supergroup_denied(self):
        update = make_update(user_id=12345678, chat_type="supergroup")
        assert not bot.is_allowed(update)

    def test_channel_denied(self):
        update = make_update(user_id=12345678, chat_type="channel")
        assert not bot.is_allowed(update)

    def test_none_user_denied(self):
        update = MagicMock()
        update.effective_user = None
        update.effective_chat = MagicMock()
        update.effective_chat.type = "private"
        assert not bot.is_allowed(update)

    def test_none_chat_denied(self):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345678
        update.effective_chat = None
        assert not bot.is_allowed(update)

    def test_none_config_denied(self, allowed_update):
        """If config is not initialized, deny everything."""
        bot._config = None
        assert not bot.is_allowed(allowed_update)


class TestSafeTools:
    """Verify the SAFE_TOOLS set is correctly defined."""

    def test_read_only_tools_are_safe(self):
        for tool in ("Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite"):
            assert tool in bot.SAFE_TOOLS

    def test_write_tools_are_not_safe(self):
        for tool in ("Bash", "Edit", "Write", "NotebookEdit"):
            assert tool not in bot.SAFE_TOOLS
