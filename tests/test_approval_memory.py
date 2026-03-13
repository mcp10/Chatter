"""Tests for session-scoped command approval memory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from chatter import bot
from tests.conftest import make_update

pytestmark = pytest.mark.anyio


class _FakeAudit:
    def __init__(self) -> None:
        self.decisions: list[tuple[int, str, str]] = []

    def log_approval(self, chat_id: int, tool_name: str, decision: str) -> None:
        self.decisions.append((chat_id, tool_name, decision))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _setup_bot_state(mock_bot_config):
    original_config = bot._config
    original_audit = bot._audit
    bot._config = mock_bot_config
    bot._audit = None
    bot._state.clear()
    yield
    bot._state.clear()
    bot._config = original_config
    bot._audit = original_audit


async def test_request_telegram_approval_command_buttons_include_session_option():
    chat_id = 99
    captured: dict[str, list[list[str]]] = {}

    class FakeBot:
        async def send_message(self, *, chat_id: int, text: str, parse_mode, reply_markup):
            captured["buttons"] = [
                [button.text for button in row]
                for row in reply_markup.inline_keyboard
            ]
            [future] = bot.get_state(chat_id)["pending_approvals"].values()
            future.set_result(bot._APPROVAL_APPROVE_SESSION)

    decision = await bot._request_telegram_approval(
        chat_id,
        FakeBot(),
        label="Bash",
        text="<b>Tool: Bash</b>",
        allow_session_approve=True,
    )

    assert decision == bot._APPROVAL_APPROVE_SESSION
    assert captured["buttons"] == [["Approve once", "Approve for session", "Deny"]]


async def test_request_telegram_approval_non_command_buttons_stay_two_button():
    chat_id = 99
    captured: dict[str, list[list[str]]] = {}

    class FakeBot:
        async def send_message(self, *, chat_id: int, text: str, parse_mode, reply_markup):
            captured["buttons"] = [
                [button.text for button in row]
                for row in reply_markup.inline_keyboard
            ]
            [future] = bot.get_state(chat_id)["pending_approvals"].values()
            future.set_result(bot._APPROVAL_APPROVE)

    decision = await bot._request_telegram_approval(
        chat_id,
        FakeBot(),
        label="FileChange",
        text="<b>Tool: FileChange</b>",
        allow_session_approve=False,
    )

    assert decision == bot._APPROVAL_APPROVE
    assert captured["buttons"] == [["Approve", "Deny"]]


async def test_claude_bash_approve_for_session_then_auto_approve(monkeypatch):
    chat_id = 7
    session = bot._new_backend_session()
    audit = _FakeAudit()
    bot._audit = audit

    request_approval = AsyncMock(side_effect=[bot._APPROVAL_APPROVE_SESSION])
    monkeypatch.setattr(bot, "_request_telegram_approval", request_approval)

    command_key = bot._claude_command_approval_key("Bash", {"command": "  pytest -q  "})

    first = await bot._decide_telegram_approval(
        chat_id,
        object(),
        session=session,
        label="Bash",
        text="<b>Tool: Bash</b>",
        audit_tool_name="Bash",
        command_key=command_key,
    )
    second = await bot._decide_telegram_approval(
        chat_id,
        object(),
        session=session,
        label="Bash",
        text="<b>Tool: Bash</b>",
        audit_tool_name="Bash",
        command_key=command_key,
    )

    assert first == bot._APPROVAL_APPROVE_SESSION
    assert second == bot._APPROVAL_APPROVE
    assert request_approval.await_count == 1
    assert session["approved_command_keys"] == {"pytest -q"}
    assert audit.decisions == [
        (chat_id, "Bash", "approved_for_session"),
        (chat_id, "Bash", "auto_approved_session"),
    ]


async def test_claude_bash_approve_once_prompts_again(monkeypatch):
    session = bot._new_backend_session()
    request_approval = AsyncMock(
        side_effect=[bot._APPROVAL_APPROVE, bot._APPROVAL_APPROVE]
    )
    monkeypatch.setattr(bot, "_request_telegram_approval", request_approval)

    command_key = bot._claude_command_approval_key("Bash", {"command": "pytest -q"})

    first = await bot._decide_telegram_approval(
        7,
        object(),
        session=session,
        label="Bash",
        text="<b>Tool: Bash</b>",
        audit_tool_name="Bash",
        command_key=command_key,
    )
    second = await bot._decide_telegram_approval(
        7,
        object(),
        session=session,
        label="Bash",
        text="<b>Tool: Bash</b>",
        audit_tool_name="Bash",
        command_key=command_key,
    )

    assert first == bot._APPROVAL_APPROVE
    assert second == bot._APPROVAL_APPROVE
    assert request_approval.await_count == 2
    assert session["approved_command_keys"] == set()


async def test_codex_command_approve_for_session_then_auto_approve(monkeypatch):
    session = bot._new_backend_session()
    request_approval = AsyncMock(side_effect=[bot._APPROVAL_APPROVE_SESSION])
    monkeypatch.setattr(bot, "_request_telegram_approval", request_approval)

    params = {"command": "zsh -lc 'pytest -q'"}

    first = await bot._decide_codex_approval(
        7,
        object(),
        session=session,
        method="item/commandExecution/requestApproval",
        params=params,
        known_item=None,
    )
    second = await bot._decide_codex_approval(
        7,
        object(),
        session=session,
        method="item/commandExecution/requestApproval",
        params=params,
        known_item=None,
    )

    assert first == bot._APPROVAL_APPROVE_SESSION
    assert second == bot._APPROVAL_APPROVE
    assert request_approval.await_count == 1
    assert session["approved_command_keys"] == {"pytest -q"}


async def test_different_command_strings_still_prompt(monkeypatch):
    session = bot._new_backend_session()
    request_approval = AsyncMock(
        side_effect=[bot._APPROVAL_APPROVE_SESSION, bot._APPROVAL_APPROVE]
    )
    monkeypatch.setattr(bot, "_request_telegram_approval", request_approval)

    first_params = {"command": "zsh -lc 'pytest -q'"}
    second_params = {"command": "zsh -lc 'pytest tests/test_auth.py -q'"}

    await bot._decide_codex_approval(
        7,
        object(),
        session=session,
        method="item/commandExecution/requestApproval",
        params=first_params,
        known_item=None,
    )
    await bot._decide_codex_approval(
        7,
        object(),
        session=session,
        method="item/commandExecution/requestApproval",
        params=second_params,
        known_item=None,
    )

    assert request_approval.await_count == 2
    assert session["approved_command_keys"] == {"pytest -q"}


async def test_new_cmd_clears_active_backend_approved_commands():
    chat_id = 77
    update = make_update(chat_id=chat_id)
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = []

    state = bot.get_state(chat_id)
    claude_session = bot._get_backend_session(state, bot.AGENT_CLAUDE)
    codex_session = bot._get_backend_session(state, bot.AGENT_CODEX)
    claude_session["approved_command_keys"].add("pytest -q")
    codex_session["approved_command_keys"].add("npm test")

    await bot.new_cmd(update, context)

    assert bot._get_backend_session(state, bot.AGENT_CLAUDE)["approved_command_keys"] == set()
    assert bot._get_backend_session(state, bot.AGENT_CODEX)["approved_command_keys"] == {"npm test"}


async def test_backend_sessions_keep_approval_caches_separate(monkeypatch):
    chat_id = 88
    state = bot.get_state(chat_id)
    claude_session = bot._get_backend_session(state, bot.AGENT_CLAUDE)
    codex_session = bot._get_backend_session(state, bot.AGENT_CODEX)
    request_approval = AsyncMock(
        side_effect=[bot._APPROVAL_APPROVE_SESSION, bot._APPROVAL_APPROVE_SESSION]
    )
    monkeypatch.setattr(bot, "_request_telegram_approval", request_approval)

    await bot._decide_telegram_approval(
        chat_id,
        object(),
        session=claude_session,
        label="Bash",
        text="<b>Tool: Bash</b>",
        audit_tool_name="Bash",
        command_key=bot._claude_command_approval_key("Bash", {"command": "pytest -q"}),
    )
    await bot._decide_codex_approval(
        chat_id,
        object(),
        session=codex_session,
        method="item/commandExecution/requestApproval",
        params={"command": "zsh -lc 'pytest -q'"},
        known_item=None,
    )

    assert request_approval.await_count == 2
    assert claude_session["approved_command_keys"] == {"pytest -q"}
    assert codex_session["approved_command_keys"] == {"pytest -q"}


async def test_codex_file_changes_still_prompt_every_time(monkeypatch):
    session = bot._new_backend_session()
    request_approval = AsyncMock(
        side_effect=[bot._APPROVAL_APPROVE, bot._APPROVAL_APPROVE]
    )
    monkeypatch.setattr(bot, "_request_telegram_approval", request_approval)

    params = {"reason": "Update README"}
    known_item = {
        "changes": [
            {
                "path": "README.md",
                "kind": {"type": "update"},
                "diff": "@@ -1 +1 @@\n-old\n+new",
            }
        ]
    }

    await bot._decide_codex_approval(
        7,
        object(),
        session=session,
        method="item/fileChange/requestApproval",
        params=params,
        known_item=known_item,
    )
    await bot._decide_codex_approval(
        7,
        object(),
        session=session,
        method="item/fileChange/requestApproval",
        params=params,
        known_item=known_item,
    )

    assert request_approval.await_count == 2
    assert session["approved_command_keys"] == set()


async def test_codex_network_approvals_still_prompt_every_time(monkeypatch):
    session = bot._new_backend_session()
    request_approval = AsyncMock(
        side_effect=[bot._APPROVAL_APPROVE, bot._APPROVAL_APPROVE]
    )
    monkeypatch.setattr(bot, "_request_telegram_approval", request_approval)

    params = {
        "command": "zsh -lc 'curl https://example.com'",
        "networkApprovalContext": {"host": "example.com"},
    }

    await bot._decide_codex_approval(
        7,
        object(),
        session=session,
        method="item/commandExecution/requestApproval",
        params=params,
        known_item=None,
    )
    await bot._decide_codex_approval(
        7,
        object(),
        session=session,
        method="item/commandExecution/requestApproval",
        params=params,
        known_item=None,
    )

    assert request_approval.await_count == 2
    assert session["approved_command_keys"] == set()
