#!/usr/bin/env python3
"""Telegram bot that bridges messages to a local Codex or Claude agent."""

from __future__ import annotations

import asyncio
import html as _html
import os
import json
import re
import shlex
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import colorama

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import (
    AGENT_CLAUDE,
    AGENT_CODEX,
    SUPPORTED_AGENT_BACKENDS,
    agent_label,
    normalize_agent_backend,
)
from .claude_auth import format_claude_auth_error, get_claude_auth_status
from .codex_app_server import CodexAppServerClient, CodexAppServerError
from .codex_auth import format_codex_auth_error, get_codex_auth_status

# ---------------------------------------------------------------------------
# Runtime config (injected by run_bot before the event loop starts)
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    bot_token: str
    allowed_user_id: int
    repo_path: str
    repo_name: str
    agent_backend: str

_config: BotConfig | None = None


# ---------------------------------------------------------------------------
# Terminal logging helpers
# ---------------------------------------------------------------------------

# Enable ANSI colors on Windows with a fallback for older colorama versions.
if hasattr(colorama, "just_fix_windows_console"):
    colorama.just_fix_windows_console()
else:
    colorama.init()

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_CYAN   = "\033[36m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_BLUE   = "\033[34m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_info(msg: str) -> None:
    print(f"{_DIM}{_ts()}{_RESET}  {msg}")


def log_user(user_id: int, text: str) -> None:
    print(f"{_DIM}{_ts()}{_RESET}  {_CYAN}{_BOLD}[user {user_id}]{_RESET} {text}")


def log_bot(text: str) -> None:
    preview = _tail(text.strip(), 1200)
    print(f"{_DIM}{_ts()}{_RESET}  {_GREEN}{_BOLD}[bot → user]{_RESET} {preview}")


def log_startup(user_id: int, repo: str, backend: str) -> None:
    label = agent_label(backend)
    print(
        f"\n{_GREEN}{_BOLD}Bot started.{_RESET}  "
        f"agent={_BOLD}{label}{_RESET}  user={_BOLD}{user_id}{_RESET}  repo={repo}\n"
    )


EDIT_INTERVAL = 2.0       # seconds between live message edits
MAX_MSG_LEN = 3500        # headroom for HTML escaping overhead (Telegram limit is 4096)
APPROVAL_TIMEOUT = 600    # seconds to wait for user approval before auto-deny (10 min)
MAX_APPROVAL_PREVIEW = 1200  # keep approval prompts comfortably under Telegram limits

_APPROVAL_APPROVE = "approve"
_APPROVAL_DENY = "deny"
_APPROVAL_CANCEL = "cancel"

# Tools that are auto-approved (read-only / non-destructive)
SAFE_TOOLS: set[str] = {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite"}

# Callback data prefixes for approval inline keyboard buttons
_CB_APPROVE = "ap"
_CB_DENY    = "dn"

# Cancel reason used when a newer prompt interrupts the current run
_INTERRUPT_REASON = "Interrupted by a newer prompt."


# ---------------------------------------------------------------------------
# Per-chat state
# ---------------------------------------------------------------------------

_state: dict = {}


def _new_backend_session() -> dict[str, Any]:
    return {
        "session_id": None,
        "session_started": False,
    }


def _active_backend(s: dict[str, Any]) -> str:
    override = s.get("backend_override")
    if override:
        return override
    if _config is None:
        raise RuntimeError("Bot config not initialized")
    return _config.agent_backend


def _get_backend_session(s: dict[str, Any], backend: str) -> dict[str, Any]:
    sessions = s.setdefault("backend_sessions", {})
    return sessions.setdefault(backend, _new_backend_session())


def _reset_backend_session(s: dict[str, Any], backend: str) -> None:
    s.setdefault("backend_sessions", {})[backend] = _new_backend_session()


def _describe_backend_session(s: dict[str, Any], backend: str) -> str:
    session = _get_backend_session(s, backend)
    if session.get("session_id") or session.get("session_started"):
        return "active"
    return "fresh"


def _format_agent_status(s: dict[str, Any]) -> str:
    default_backend = _config.agent_backend
    active_backend = _active_backend(s)
    override = s.get("backend_override")
    lines = [
        f"Current backend: {agent_label(active_backend)}",
        f"Repo default: {agent_label(default_backend)}",
    ]
    if override:
        lines.append(f"Chat override: {agent_label(override)}")
    else:
        lines.append("Chat override: none")

    for backend in SUPPORTED_AGENT_BACKENDS:
        lines.append(
            f"{agent_label(backend)} session: {_describe_backend_session(s, backend)}"
        )

    lines.append("")
    lines.append("Use /agent codex, /agent claude, or /agent default.")
    return "\n".join(lines)


def get_state(chat_id: int) -> dict:
    if chat_id not in _state:
        _state[chat_id] = {
            "cwd": _config.repo_path,
            "lock": asyncio.Lock(),
            "running": False,
            "running_backend": None,
            "run_task": None,
            "agent_process": None,
            "codex_client": None,
            "backend_override": None,
            "backend_sessions": {
                AGENT_CODEX: _new_backend_session(),
                AGENT_CLAUDE: _new_backend_session(),
            },
            "cancelled": False,
            "cancel_reason": None,
            "pending_approvals": {},   # approval_id -> asyncio.Future[str]
        }
    return _state[chat_id]


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if _config is None or user is None or chat is None:
        return False
    return user.id == _config.allowed_user_id and chat.type == "private"


_PATH_KEYS = {"path", "file_path", "cwd", "notebook_path", "root_path"}
_PATH_LIST_KEYS = {"paths", "file_paths"}
_GLOB_META_CHARS = set("*?[]{}")
_EMBEDDED_ABS_PATH_RE = re.compile(
    r"(?:^|[\\s'\"=:(,\\[{])(/[^\\s'\"`|&;()<>]+|~(?:/[^\\s'\"`|&;()<>]*)?)"
)


def _repo_root() -> Path:
    if _config is None:
        raise RuntimeError("Bot config not initialized")
    return Path(_config.repo_path).resolve()


def _resolve_candidate_path(raw_path: str, repo_root: Path) -> Path:
    expanded = os.path.expanduser(raw_path.strip())
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve(strict=False)


def _is_within_repo(path: Path, repo_root: Path) -> bool:
    try:
        path.relative_to(repo_root)
        return True
    except ValueError:
        return False


def _glob_anchor(pattern: str) -> str:
    first_glob = len(pattern)
    for idx, char in enumerate(pattern):
        if char in _GLOB_META_CHARS:
            first_glob = idx
            break
    anchor = pattern[:first_glob].strip()
    return anchor or "."


def _iter_tool_paths(tool_name: str, input_data: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    for key in _PATH_KEYS:
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())

    for key in _PATH_LIST_KEYS:
        value = input_data.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    paths.append(item.strip())

    if tool_name == "Glob":
        pattern = input_data.get("pattern")
        if isinstance(pattern, str) and pattern.strip():
            paths.append(_glob_anchor(pattern))

    return paths


def _bash_within_repo(command: str, repo_root: Path) -> tuple[bool, str | None]:
    cmd = command.strip()
    if not cmd:
        return True, None

    if re.search(r"(^|[;&|()\s])cd\s+([/~]|\.\.)", cmd):
        return False, "Bash command attempted to leave the repository."

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        tokens = cmd.split()

    prev = ""
    for token in tokens:
        if token in {"..", "~"} or token.startswith("../") or token.startswith("~") or "/../" in token or token.endswith("/.."):
            return False, f"Bash path '{token}' escapes the repository."

        # Catch absolute/home paths embedded in arguments, for example:
        # python -c "open('/etc/passwd')"
        if "://" not in token:
            for match in _EMBEDDED_ABS_PATH_RE.finditer(token):
                raw_path = match.group(1).rstrip(".,:;)]}")
                if raw_path.startswith("/") or raw_path.startswith("~"):
                    resolved = _resolve_candidate_path(raw_path, repo_root)
                    if not _is_within_repo(resolved, repo_root):
                        return False, f"Bash path '{raw_path}' is outside the repository."

        if token.startswith("/"):
            resolved = _resolve_candidate_path(token, repo_root)
            if not _is_within_repo(resolved, repo_root):
                return False, f"Bash path '{token}' is outside the repository."

        if prev == "cd":
            resolved = _resolve_candidate_path(token, repo_root)
            if not _is_within_repo(resolved, repo_root):
                return False, f"Bash cd target '{token}' is outside the repository."

        prev = token

    return True, None


def _repo_scope_violation(tool_name: str, input_data: dict[str, Any]) -> str | None:
    repo_root = _repo_root()

    if tool_name == "Bash":
        command = str(input_data.get("command", ""))
        allowed, reason = _bash_within_repo(command, repo_root)
        if not allowed:
            return reason
        return None

    for raw_path in _iter_tool_paths(tool_name, input_data):
        resolved = _resolve_candidate_path(raw_path, repo_root)
        if not _is_within_repo(resolved, repo_root):
            return f"{tool_name} requested '{raw_path}', which is outside {_config.repo_path}."

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def trim(text: str) -> str:
    """Keep last MAX_MSG_LEN chars so the message always shows the latest output."""
    if len(text) > MAX_MSG_LEN:
        text = "…" + text[-MAX_MSG_LEN:]
    return text


def _tail(text: str, limit: int) -> str:
    if len(text) > limit:
        return "…" + text[-limit:]
    return text


def _looks_like_error_text(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "error:",
        "failed to",
        '"type":"error"',
        '"type": "error"',
        "authentication_error",
        "oauth token has expired",
        "permission denied",
        "timed out",
    )
    return any(marker in lowered for marker in markers)


def _log_output_preview(label: str, text: str, *, color: str = _YELLOW) -> None:
    preview = _tail(text.strip(), 1200)
    if preview:
        log_info(f"{color}{label}:{_RESET}\n{preview}")


def to_pre(text: str) -> str:
    """Wrap text in an HTML <pre> block — used for live streaming previews."""
    return f"<pre>{_html.escape(text)}</pre>"


def md_to_html(text: str) -> str:
    """Convert Claude's markdown output to Telegram HTML.

    Handles fenced code blocks, inline code, bold, and italic.
    Everything else is HTML-escaped and sent as plain text so that
    tables and prose render naturally instead of as raw monospace.
    """
    parts: list[str] = []

    # Split on fenced code blocks, preserving them as a capture group.
    segments = re.split(r"(```(?:[^\n]*)?\n.*?```)", text, flags=re.DOTALL)
    for seg in segments:
        m = re.match(r"```([^\n]*)\n(.*?)```", seg, flags=re.DOTALL)
        if m:
            code = _html.escape(m.group(2).rstrip())
            parts.append(f"<pre><code>{code}</code></pre>")
            continue

        # Split on markdown tables (consecutive lines starting with |).
        table_segments = re.split(r"(\n?\|[^\n]*\|(?:\n\|[^\n]*\|)+)", seg)
        for tseg in table_segments:
            if re.match(r"\n?\|", tseg) and tseg.strip().endswith("|"):
                # Convert table to list format for readability on mobile.
                data_rows = [
                    line for line in tseg.strip().splitlines()
                    if not re.match(r"^[\s|\-:]+$", line.strip())
                ]
                if len(data_rows) >= 2:
                    headers = [c.strip() for c in data_rows[0].strip("|").split("|")]
                    list_parts: list[str] = []
                    for row in data_rows[1:]:
                        cells = [c.strip() for c in row.strip("|").split("|")]
                        lines = []
                        for i, (h, c) in enumerate(zip(headers, cells)):
                            if i == 0:
                                lines.append(f"<b>{_html.escape(c)}</b>")
                            else:
                                lines.append(f"  {_html.escape(h)}: {_html.escape(c)}")
                        list_parts.append("\n".join(lines))
                    parts.append("\n\n".join(list_parts))
                else:
                    parts.append(f"<pre>{_html.escape(tseg.strip())}</pre>")
                continue

            # Split on inline code spans.
            inline_segments = re.split(r"(`[^`]+`)", tseg)
            for part in inline_segments:
                if part.startswith("`") and part.endswith("`") and len(part) > 1:
                    parts.append(f"<code>{_html.escape(part[1:-1])}</code>")
                    continue
                # Escape then apply bold / italic.
                escaped = _html.escape(part)
                escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
                escaped = re.sub(r"\*(.+?)\*", r"<i>\1</i>", escaped, flags=re.DOTALL)
                parts.append(escaped)

    return "".join(parts)


async def safe_edit(msg, text: str) -> None:
    for _ in range(5):
        try:
            await msg.edit_text(to_pre(trim(text)), parse_mode=ParseMode.HTML)
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except BadRequest as e:
            log_info(f"safe_edit: {e}")
            return


async def _typing_loop(chat_id: int, bot, stop_event: asyncio.Event) -> None:
    """Send typing action every 4 s until stop_event is set (indicator lasts ~5 s)."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass


async def send_final(status_msg, reply_to, text: str) -> None:
    """Delete the status indicator and send the answer as fresh message(s)."""
    try:
        await status_msg.delete()
    except Exception:
        pass
    if not text:
        log_bot("(no output)")
        await reply_to.reply_text("(no output)")
        return
    log_bot(text)
    chunks = [text[i:i + MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)]
    for chunk in chunks:
        await reply_to.reply_text(md_to_html(chunk), parse_mode=ParseMode.HTML)


async def _ensure_claude_auth(update: Update) -> bool:
    """Fail fast when the local Claude Code CLI is logged out."""
    status = await asyncio.to_thread(get_claude_auth_status)
    if status.ok:
        return True

    message = format_claude_auth_error(status)
    log_info(f"{_RED}Claude auth check failed:{_RESET}\n{message}")
    if update.message is not None:
        await update.message.reply_text(message)
    return False


async def _ensure_codex_auth(update: Update) -> bool:
    """Fail fast when the local Codex CLI is logged out."""
    status = await asyncio.to_thread(get_codex_auth_status)
    if status.ok:
        return True

    message = format_codex_auth_error(status)
    log_info(f"{_RED}Codex auth check failed:{_RESET}\n{message}")
    if update.message is not None:
        await update.message.reply_text(message)
    return False


async def _ensure_agent_auth(update: Update, backend: str) -> bool:
    """Fail fast when the selected agent CLI is unavailable or logged out."""
    if backend == AGENT_CODEX:
        return await _ensure_codex_auth(update)
    return await _ensure_claude_auth(update)


def _get_codex_client(s: dict[str, Any]) -> CodexAppServerClient:
    client = s.get("codex_client")
    if client is None:
        client = CodexAppServerClient(
            s["cwd"],
            stderr_callback=lambda line: log_info(f"{_RED}CODEX STDERR:{_RESET} {line}"),
        )
        s["codex_client"] = client
    return client


# ---------------------------------------------------------------------------
# Tool approval helpers
# ---------------------------------------------------------------------------

class ApprovalRequestError(RuntimeError):
    """Raised when the Telegram approval prompt cannot be delivered."""


def _format_tool_request(tool_name: str, input_data: dict[str, Any]) -> str:
    """Format a tool approval request for Telegram display."""
    parts = [f"<b>Tool: {_html.escape(tool_name)}</b>\n"]

    if tool_name == "Bash":
        cmd = input_data.get("command", "")
        desc = input_data.get("description", "")
        if desc:
            parts.append(f"{_html.escape(_tail(desc, 300))}\n")
        parts.append(f"<pre>{_html.escape(_tail(cmd, MAX_APPROVAL_PREVIEW))}</pre>")

    elif tool_name in ("Write", "Edit"):
        fp = input_data.get("file_path", "")
        parts.append(f"File: <code>{_html.escape(fp)}</code>\n")
        if tool_name == "Edit":
            old = input_data.get("old_string", "")[:300]
            new = input_data.get("new_string", "")[:300]
            parts.append(f"<pre>- {_html.escape(old)}\n+ {_html.escape(new)}</pre>")
        else:
            content = _tail(input_data.get("content", ""), 500)
            parts.append(f"<pre>{_html.escape(content)}</pre>")

    else:
        summary = _tail(json.dumps(input_data, indent=2, default=str), 500)
        parts.append(f"<pre>{_html.escape(summary)}</pre>")

    return "\n".join(parts)


def _format_codex_command_request(params: dict[str, Any]) -> str:
    network_context = params.get("networkApprovalContext")
    if isinstance(network_context, dict) and network_context:
        return _format_tool_request("SandboxNetworkAccess", network_context)

    input_data: dict[str, Any] = {
        "command": _display_shell_command(str(params.get("command") or "")),
    }
    reason = params.get("reason")
    if isinstance(reason, str) and reason.strip():
        input_data["description"] = reason.strip()
    return _format_tool_request("Bash", input_data)


def _format_codex_legacy_command_request(params: dict[str, Any]) -> str:
    command = params.get("command")
    if isinstance(command, list):
        rendered = shlex.join([str(part) for part in command])
    else:
        rendered = str(command or "")

    input_data: dict[str, Any] = {
        "command": _display_shell_command(rendered),
    }
    reason = params.get("reason")
    if isinstance(reason, str) and reason.strip():
        input_data["description"] = reason.strip()
    return _format_tool_request("Bash", input_data)


def _format_codex_file_change_request(
    params: dict[str, Any],
    known_item: dict[str, Any] | None,
) -> str:
    parts = ["<b>Tool: FileChange</b>\n"]

    reason = params.get("reason")
    if isinstance(reason, str) and reason.strip():
        parts.append(f"{_html.escape(_tail(reason.strip(), 300))}\n")

    grant_root = params.get("grantRoot")
    if isinstance(grant_root, str) and grant_root.strip():
        parts.append(f"Grant root: <code>{_html.escape(grant_root.strip())}</code>\n")

    changes = known_item.get("changes") if isinstance(known_item, dict) else None
    if isinstance(changes, list) and changes:
        preview_parts: list[str] = []
        for change in changes[:4]:
            if not isinstance(change, dict):
                continue
            path = str(change.get("path") or "")
            kind = change.get("kind")
            if isinstance(kind, dict):
                kind_label = str(kind.get("type") or "update")
            else:
                kind_label = str(kind or "update")
            diff = str(change.get("diff") or "").strip()
            snippet = f"{kind_label}: {path}"
            if diff:
                snippet = f"{snippet}\n{diff}"
            preview_parts.append(snippet)

        if preview_parts:
            preview = _tail("\n\n".join(preview_parts), MAX_APPROVAL_PREVIEW)
            parts.append(f"<pre>{_html.escape(preview)}</pre>")
            return "\n".join(parts)

    summary = _tail(json.dumps(params, indent=2, default=str), 500)
    parts.append(f"<pre>{_html.escape(summary)}</pre>")
    return "\n".join(parts)


def _format_codex_legacy_file_change_request(params: dict[str, Any]) -> str:
    parts = ["<b>Tool: FileChange</b>\n"]

    reason = params.get("reason")
    if isinstance(reason, str) and reason.strip():
        parts.append(f"{_html.escape(_tail(reason.strip(), 300))}\n")

    grant_root = params.get("grantRoot")
    if isinstance(grant_root, str) and grant_root.strip():
        parts.append(f"Grant root: <code>{_html.escape(grant_root.strip())}</code>\n")

    file_changes = params.get("fileChanges")
    if isinstance(file_changes, dict) and file_changes:
        preview_parts: list[str] = []
        for path, change in list(file_changes.items())[:4]:
            if not isinstance(change, dict):
                continue

            change_type = str(change.get("type") or "update")
            snippet = f"{change_type}: {path}"
            if change_type == "update":
                move_path = str(change.get("move_path") or "").strip()
                if move_path:
                    snippet = f"{snippet} -> {move_path}"
                diff = str(change.get("unified_diff") or "").strip()
                if diff:
                    snippet = f"{snippet}\n{diff}"
            else:
                content = str(change.get("content") or "").strip()
                if content:
                    snippet = f"{snippet}\n{content}"
            preview_parts.append(snippet)

        if preview_parts:
            preview = _tail("\n\n".join(preview_parts), MAX_APPROVAL_PREVIEW)
            parts.append(f"<pre>{_html.escape(preview)}</pre>")
            return "\n".join(parts)

    summary = _tail(json.dumps(params, indent=2, default=str), 500)
    parts.append(f"<pre>{_html.escape(summary)}</pre>")
    return "\n".join(parts)


def _format_codex_approval_request(
    method: str,
    params: dict[str, Any],
    known_item: dict[str, Any] | None,
) -> str:
    if method == "item/commandExecution/requestApproval":
        return _format_codex_command_request(params)
    if method == "execCommandApproval":
        return _format_codex_legacy_command_request(params)
    if method == "item/fileChange/requestApproval":
        return _format_codex_file_change_request(params, known_item)
    return _format_codex_legacy_file_change_request(params)


def _codex_approval_label(method: str) -> str:
    if method in {"item/commandExecution/requestApproval", "execCommandApproval"}:
        return "Codex command"
    return "Codex file change"


def _codex_approval_result(method: str, decision: str) -> str:
    if method in {"execCommandApproval", "applyPatchApproval"}:
        if decision == _APPROVAL_APPROVE:
            return "approved"
        if decision == _APPROVAL_CANCEL:
            return "abort"
        return "denied"

    if decision == _APPROVAL_APPROVE:
        return "accept"
    if decision == _APPROVAL_CANCEL:
        return "cancel"
    return "decline"


def _codex_error_text(error: Any) -> str | None:
    if not isinstance(error, dict):
        if error is None:
            return None
        return str(error)

    parts: list[str] = []
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())

    details = error.get("additionalDetails")
    if isinstance(details, str) and details.strip():
        parts.append(details.strip())

    if parts:
        return "\n\n".join(parts)

    codex_error_info = error.get("codexErrorInfo")
    if codex_error_info is not None:
        return json.dumps(codex_error_info, default=str)
    return json.dumps(error, default=str)


def _codex_dynamic_tool_result(tool_name: str) -> dict[str, Any]:
    normalized = tool_name.strip().lower()
    if normalized == "update_plan":
        return {"success": True, "contentItems": []}

    return {
        "success": True,
        "contentItems": [
            {
                "type": "inputText",
                "text": (
                    f"Tool '{tool_name or 'unknown'}' is unavailable in Chatter Telegram mode. "
                    "Continue without it and answer using the available Codex tools."
                ),
            }
        ],
    }


def _codex_failed_item_text(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("type") or "")

    if item_type == "dynamicToolCall":
        tool_name = str(item.get("tool") or "unknown")
        if item.get("success") is False or str(item.get("status") or "") == "failed":
            return f"Codex dynamic tool '{tool_name}' failed."
        return None

    if item_type == "mcpToolCall":
        error = item.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if item.get("error") is not None:
            return f"MCP tool '{item.get('tool')}' failed."
        return None

    if item_type == "commandExecution" and str(item.get("status") or "") in {"failed", "declined"}:
        command = _display_shell_command(str(item.get("command") or ""))
        exit_code = item.get("exitCode")
        if exit_code is not None:
            return f"Command failed with exit code {exit_code}: {command}"
        return f"Command failed: {command}"

    if item_type == "fileChange" and str(item.get("status") or "") == "failed":
        return "Codex file change failed."

    return None


async def _request_telegram_approval(
    chat_id: int,
    bot,
    *,
    label: str,
    text: str,
) -> str:
    s = get_state(chat_id)
    approval_id = uuid.uuid4().hex[:8]
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    s["pending_approvals"][approval_id] = future

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"{_CB_APPROVE}:{approval_id}"),
            InlineKeyboardButton("Deny", callback_data=f"{_CB_DENY}:{approval_id}"),
        ]
    ])

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as exc:
        if not future.done():
            future.cancel()
        s["pending_approvals"].pop(approval_id, None)
        raise ApprovalRequestError(f"Could not send approval request: {exc}") from exc

    log_info(f"{_YELLOW}Approval requested:{_RESET} {label} [{approval_id}]")

    deadline = asyncio.get_running_loop().time() + APPROVAL_TIMEOUT
    try:
        while not future.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                log_info(f"Approval timed out: {approval_id}")
                try:
                    await bot.send_message(chat_id=chat_id, text="Timed out — auto-denied.")
                except Exception:
                    pass
                if not future.done():
                    future.set_result(_APPROVAL_DENY)
                break
            await asyncio.sleep(min(0.25, remaining))

        decision = future.result()
    finally:
        s["pending_approvals"].pop(approval_id, None)

    if not isinstance(decision, str):
        return _APPROVAL_DENY
    return decision


def _make_can_use_tool(chat_id: int, bot):
    """Factory that creates a can_use_tool callback bound to a specific chat."""
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    async def can_use_tool(
        tool_name: str, input_data: dict[str, Any], context: Any
    ) -> PermissionResultAllow | PermissionResultDeny:
        # Entire body wrapped so exceptions never leak into the SDK TaskGroup
        try:
            return await _can_use_tool_inner(tool_name, input_data)
        except Exception as e:
            log_info(f"{_RED}can_use_tool crashed:{_RESET} {type(e).__name__}: {e}")
            return PermissionResultDeny(message=f"Internal error: {e}")

    async def _can_use_tool_inner(
        tool_name: str, input_data: dict[str, Any]
    ) -> PermissionResultAllow | PermissionResultDeny:
        log_info(f"{_YELLOW}Tool request:{_RESET} {tool_name}")
        s = get_state(chat_id)

        violation = _repo_scope_violation(tool_name, input_data)
        if violation:
            log_info(f"{_RED}Blocked (outside repo):{_RESET} {tool_name} - {violation}")
            return PermissionResultDeny(message=violation)

        # Auto-approve safe tools
        if tool_name in SAFE_TOOLS:
            log_info(f"{_GREEN}Auto-approved:{_RESET} {tool_name}")
            return PermissionResultAllow(updated_input=input_data)

        text = _format_tool_request(tool_name, input_data)
        try:
            decision = await _request_telegram_approval(
                chat_id,
                bot,
                label=tool_name,
                text=text,
            )
        except ApprovalRequestError as exc:
            log_info(f"{_RED}Failed to send approval message:{_RESET} {exc}")
            return PermissionResultDeny(message=str(exc))

        if decision == _APPROVAL_APPROVE:
            log_info(f"{_GREEN}Approved:{_RESET} {tool_name}")
            return PermissionResultAllow(updated_input=input_data)
        elif decision == _APPROVAL_CANCEL:
            log_info(f"{_RED}Cancelled:{_RESET} {tool_name}")
            return PermissionResultDeny(message=f"User cancelled {tool_name}")
        else:
            log_info(f"{_RED}Denied:{_RESET} {tool_name}")
            return PermissionResultDeny(message=f"User denied {tool_name}")

    return can_use_tool


def _cancel_run(s: dict, *, reason: str) -> bool:
    """Cancel the active run task and unblock any pending approvals."""
    had_run = bool(s.get("running"))
    s["cancelled"] = True
    s["cancel_reason"] = reason
    running_backend = s.get("running_backend")

    for future in s["pending_approvals"].values():
        if not future.done():
            future.set_result(_APPROVAL_CANCEL)
    s["pending_approvals"].clear()

    run_task = s.get("run_task")
    if running_backend != AGENT_CODEX and run_task and not run_task.done():
        run_task.cancel()
        had_run = True

    process = s.get("agent_process")
    if running_backend != AGENT_CODEX and process and process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
        had_run = True

    return had_run



# ---------------------------------------------------------------------------
# Callback handler for inline keyboard approval buttons
# ---------------------------------------------------------------------------

async def _approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for tool approval."""
    cb = update.callback_query
    if cb is None:
        return
    with suppress(BadRequest):
        await cb.answer()

    if not is_allowed(update):
        return

    data = cb.data or ""
    if ":" not in data:
        return
    log_info(f"{_YELLOW}Approval callback:{_RESET} {data}")
    chat_id = update.effective_chat.id
    s = get_state(chat_id)

    if data.startswith(f"{_CB_APPROVE}:") or data.startswith(f"{_CB_DENY}:"):
        prefix, approval_id = data.split(":", 1)
        decision = _APPROVAL_APPROVE if prefix == _CB_APPROVE else _APPROVAL_DENY
        future = s["pending_approvals"].get(approval_id)
        if future and not future.done():
            future.set_result(decision)
            label = "APPROVED" if decision == _APPROVAL_APPROVE else "DENIED"
            try:
                await cb.edit_message_text(
                    f"{cb.message.text_html}\n\n<b>{label}</b>",
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass
        else:
            log_info(f"{_YELLOW}Approval expired/missing:{_RESET} [{approval_id}]")
            try:
                await cb.edit_message_text("(expired)")
            except BadRequest:
                pass


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    s = get_state(update.effective_chat.id)
    label = agent_label(_active_backend(s))
    await update.message.reply_text(
        f"{label} agent bridge ready.\n"
        f"Repo: {_config.repo_name} ({s['cwd']})\n\n"
        f"/agent  — show or switch backend\n"
        f"/cancel  — stop the running agent\n"
        f"/new     — start a fresh conversation\n\n"
        f"Just send a prompt to start."
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    s = get_state(update.effective_chat.id)
    if _cancel_run(s, reason="Cancelled by user."):
        await update.message.reply_text("Cancelling…")
    else:
        await update.message.reply_text("No agent running.")


async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    s = get_state(update.effective_chat.id)
    backend = _active_backend(s)
    _reset_backend_session(s, backend)
    await update.message.reply_text(
        f"{agent_label(backend)} session reset. Next prompt will start a new conversation."
    )


async def agent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    s = get_state(update.effective_chat.id)
    args = context.args or []
    if not args:
        await update.message.reply_text(_format_agent_status(s))
        return

    if len(args) != 1:
        await update.message.reply_text("Usage: /agent [codex|claude|default]")
        return

    raw_target = args[0].strip()
    use_default = raw_target.lower() == "default"
    try:
        target_backend = (
            _config.agent_backend
            if use_default
            else normalize_agent_backend(raw_target)
        )
    except ValueError:
        await update.message.reply_text("Usage: /agent [codex|claude|default]")
        return

    if not await _ensure_agent_auth(update, target_backend):
        return

    previous_backend = _active_backend(s)
    had_run = False
    if s.get("running"):
        had_run = _cancel_run(
            s,
            reason=f"Backend switched to {agent_label(target_backend)}.",
        )

    s["backend_override"] = None if target_backend == _config.agent_backend else target_backend

    if previous_backend == target_backend:
        message = _format_agent_status(s)
        if had_run:
            message = f"Cancelled the active run.\n\n{message}"
        await update.message.reply_text(message)
        return

    lines = [
        f"Switched backend from {agent_label(previous_backend)} to {agent_label(target_backend)}.",
    ]
    if had_run:
        lines.append("Cancelled the active run.")
    lines.append(
        f"{agent_label(target_backend)} session is {_describe_backend_session(s, target_backend)}."
    )
    lines.append("")
    lines.append(_format_agent_status(s))
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main message handler — runs the selected agent and streams output
# ---------------------------------------------------------------------------

def _display_shell_command(command: str) -> str:
    """Strip the shell wrapper Codex uses around command execution items."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return command

    if len(tokens) >= 3 and tokens[1] == "-lc" and tokens[0].endswith(("sh", "bash", "zsh")):
        return tokens[2]
    return command


def _codex_status_preview(
    latest_agent_message: str,
    current_command: str,
    current_command_output: str,
    elapsed: int,
) -> str:
    """Choose the most useful live preview for a Codex turn."""
    if latest_agent_message:
        return latest_agent_message
    if current_command:
        preview = f"$ {current_command}"
        output = current_command_output.strip()
        if output:
            preview = f"{preview}\n\n{_tail(output, 1600)}"
        return preview
    return f"⏳ Thinking… ({elapsed}s)"


def _codex_thread_params(cwd: str) -> dict[str, Any]:
    return {
        "cwd": cwd,
        "sandbox": "workspace-write",
        "approvalPolicy": "untrusted",
        "persistExtendedHistory": True,
        "config": {
            "apps": {
                "_default": {
                    "default_tools_approval_mode": "prompt",
                    "default_tools_enabled": True,
                }
            }
        },
    }


def _codex_turn_params(thread_id: str, prompt: str) -> dict[str, Any]:
    return {
        "threadId": thread_id,
        "approvalPolicy": "untrusted",
        "input": [
            {
                "type": "text",
                "text": prompt,
                "text_elements": [],
            }
        ],
    }


def _codex_turn_error_text(turn: dict[str, Any] | None) -> str | None:
    if not isinstance(turn, dict):
        return None

    return _codex_error_text(turn.get("error"))


async def _run_codex_turn(
    prompt: str,
    chat_id: int,
    bot,
    status_msg,
    start_time: float,
    s: dict,
    session: dict[str, Any],
) -> tuple[str, bool, str]:
    """Run a single Codex turn via the persistent app-server transport."""
    cwd = s["cwd"]
    client = _get_codex_client(s)
    client.drain_events()

    thread_id = str(session.get("session_id") or "")
    turn_id = ""
    latest_agent_message = ""
    current_command = ""
    current_command_output = ""
    current_command_item_id = ""
    agent_messages: dict[str, str] = {}
    command_outputs: dict[str, str] = {}
    known_items: dict[str, dict[str, Any]] = {}
    last_edit = asyncio.get_running_loop().time()
    cancelled = False
    cancel_reason = "Cancelled."
    turn_status = ""
    turn_error_text: str | None = None
    interrupt_requested = False
    last_host_request = ""

    try:
        thread_params = _codex_thread_params(cwd)
        thread_result: dict[str, Any] | None = None

        if thread_id:
            log_info(f"Resuming Codex session {_BOLD}{thread_id}{_RESET}")
            try:
                thread_result = await client.request(
                    "thread/resume",
                    {
                        **thread_params,
                        "threadId": thread_id,
                    },
                    timeout=15.0,
                )
            except CodexAppServerError as exc:
                log_info(
                    f"{_YELLOW}Codex resume failed; starting fresh instead:{_RESET} {exc}"
                )
                session["session_id"] = None
                session["session_started"] = False
                thread_id = ""

        if not thread_id:
            log_info("Starting new Codex session")
            thread_result = await client.request(
                "thread/start",
                {
                    **thread_params,
                    "experimentalRawEvents": False,
                },
                timeout=15.0,
            )

        if isinstance(thread_result, dict):
            thread = thread_result.get("thread")
            if isinstance(thread, dict):
                started_thread_id = thread.get("id")
                if isinstance(started_thread_id, str) and started_thread_id:
                    thread_id = started_thread_id
                    session["session_id"] = thread_id
                    session["session_started"] = True

        if not thread_id:
            return "Error: Codex did not return a thread id.", False, cancel_reason

        turn_result = await client.request(
            "turn/start",
            _codex_turn_params(thread_id, prompt),
            timeout=15.0,
        )
        if isinstance(turn_result, dict):
            turn = turn_result.get("turn")
            if isinstance(turn, dict):
                started_turn_id = turn.get("id")
                if isinstance(started_turn_id, str) and started_turn_id:
                    turn_id = started_turn_id

        while True:
            if s.get("cancelled") and turn_id and not interrupt_requested:
                cancelled = True
                cancel_reason = s.get("cancel_reason") or cancel_reason
                interrupt_requested = True
                log_info(f"{_YELLOW}Interrupting Codex turn:{_RESET} {turn_id}")
                await client.interrupt_turn(thread_id, turn_id)

            event: dict[str, Any] | None = None
            try:
                event = await client.next_event(timeout=0.25)
            except asyncio.TimeoutError:
                event = None

            if event is not None:
                method = str(event.get("method") or "")
                params = event.get("params")
                if not isinstance(params, dict):
                    params = {}

                event_thread_id = params.get("threadId")
                if isinstance(event_thread_id, str) and event_thread_id and event_thread_id != thread_id:
                    continue

                event_turn_id = ""
                if isinstance(params.get("turnId"), str):
                    event_turn_id = params["turnId"]
                elif isinstance(params.get("turn"), dict):
                    candidate_turn_id = params["turn"].get("id")
                    if isinstance(candidate_turn_id, str):
                        event_turn_id = candidate_turn_id
                if turn_id and event_turn_id and event_turn_id != turn_id:
                    continue

                if "id" in event and method in {
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                    "execCommandApproval",
                    "applyPatchApproval",
                }:
                    last_host_request = method
                    known_item = known_items.get(str(params.get("itemId") or ""))
                    text = _format_codex_approval_request(method, params, known_item)
                    label = _codex_approval_label(method)
                    try:
                        decision = await _request_telegram_approval(
                            chat_id,
                            bot,
                            label=label,
                            text=text,
                        )
                    except ApprovalRequestError as exc:
                        log_info(f"{_RED}Failed to send Codex approval message:{_RESET} {exc}")
                        decision = _APPROVAL_CANCEL if s.get("cancelled") else _APPROVAL_DENY

                    codex_decision = _codex_approval_result(method, decision)

                    if decision == _APPROVAL_APPROVE:
                        log_info(f"{_GREEN}Approved:{_RESET} {label}")
                    elif decision == _APPROVAL_CANCEL:
                        log_info(f"{_RED}Cancelled:{_RESET} {label}")
                    else:
                        log_info(f"{_RED}Denied:{_RESET} {label}")

                    await client.respond(
                        event["id"],
                        result={
                            "decision": codex_decision,
                        },
                    )
                    continue

                if "id" in event and method == "item/tool/requestUserInput":
                    last_host_request = method
                    log_info(f"{_YELLOW}Codex requested unsupported user input; sending empty response.{_RESET}")
                    await client.respond(event["id"], result={"answers": {}})
                    continue

                if "id" in event and method == "item/tool/call":
                    last_host_request = f"{method}:{str(params.get('tool') or 'unknown')}"
                    tool_name = str(params.get("tool") or "unknown")
                    log_info(f"{_YELLOW}Codex dynamic tool request:{_RESET} {tool_name}")
                    await client.respond(
                        event["id"],
                        result=_codex_dynamic_tool_result(tool_name),
                    )
                    continue

                if "id" in event and method == "account/chatgptAuthTokens/refresh":
                    last_host_request = method
                    log_info(
                        f"{_RED}Codex auth refresh request unsupported in Chatter:{_RESET} "
                        f"{json.dumps(params, default=str)}"
                    )
                    await client.respond(
                        event["id"],
                        error={
                            "code": -32601,
                            "message": "ChatGPT auth token refresh is not supported in Chatter.",
                        },
                    )
                    continue

                if "id" in event:
                    last_host_request = method
                    summary = _tail(json.dumps(params, default=str), 400)
                    log_info(
                        f"{_RED}Unsupported Codex server request:{_RESET} "
                        f"{method} {summary}"
                    )
                    await client.respond(
                        event["id"],
                        error={
                            "code": -32601,
                            "message": f"Unsupported Codex server request: {method}",
                        },
                    )
                    continue

                if method == "error":
                    turn_error_text = _codex_error_text(params.get("error"))
                    will_retry = bool(params.get("willRetry"))
                    if turn_error_text:
                        color = _YELLOW if will_retry else _RED
                        prefix = "Codex turn warning" if will_retry else "Codex turn error"
                        log_info(f"{color}{prefix}:{_RESET} {turn_error_text}")
                    if will_retry:
                        continue
                    turn_status = "failed"
                    if not turn_error_text:
                        turn_error_text = "Codex app-server error."
                    break

                if method == "thread/started":
                    thread = params.get("thread")
                    if isinstance(thread, dict):
                        started_thread_id = thread.get("id")
                        if isinstance(started_thread_id, str) and started_thread_id:
                            thread_id = started_thread_id
                            session["session_id"] = thread_id
                            session["session_started"] = True
                    continue

                if method == "turn/started":
                    turn = params.get("turn")
                    if isinstance(turn, dict):
                        started_turn_id = turn.get("id")
                        if isinstance(started_turn_id, str) and started_turn_id:
                            turn_id = started_turn_id
                    continue

                if method == "item/started":
                    item = params.get("item")
                    if not isinstance(item, dict):
                        continue

                    item_id = str(item.get("id") or "")
                    if item_id:
                        known_items[item_id] = item

                    item_type = item.get("type")
                    if item_type == "commandExecution":
                        current_command_item_id = item_id
                        current_command = _display_shell_command(str(item.get("command") or ""))
                        current_command_output = str(item.get("aggregatedOutput") or "")
                        log_info(f"{_BLUE}Codex command:{_RESET} {current_command}")
                    elif item_type == "dynamicToolCall":
                        log_info(
                            f"{_BLUE}Codex dynamic tool:{_RESET} "
                            f"{item.get('tool', 'unknown')}"
                        )
                    continue

                if method == "item/commandExecution/outputDelta":
                    item_id = str(params.get("itemId") or "")
                    if not item_id:
                        continue
                    command_outputs[item_id] = command_outputs.get(item_id, "") + str(params.get("delta") or "")
                    if item_id == current_command_item_id:
                        current_command_output = command_outputs[item_id]
                    continue

                if method == "item/agentMessage/delta":
                    item_id = str(params.get("itemId") or "")
                    if not item_id:
                        continue
                    agent_messages[item_id] = agent_messages.get(item_id, "") + str(params.get("delta") or "")
                    latest_agent_message = agent_messages[item_id].strip()
                    continue

                if method == "item/completed":
                    item = params.get("item")
                    if not isinstance(item, dict):
                        continue

                    item_id = str(item.get("id") or "")
                    if item_id:
                        known_items[item_id] = item

                    item_type = item.get("type")
                    if item_type == "commandExecution":
                        current_command_item_id = item_id
                        current_command = _display_shell_command(str(item.get("command") or ""))
                        current_command_output = str(item.get("aggregatedOutput") or current_command_output)
                        exit_code = item.get("exitCode")
                        log_info(
                            f"{_BLUE}Codex command done:{_RESET} "
                            f"exit={exit_code}  cmd={current_command}"
                        )
                    elif item_type == "agentMessage":
                        text = str(item.get("text") or "").strip()
                        if text:
                            agent_messages[item_id] = text
                            latest_agent_message = text
                            log_info(f"{_YELLOW}Codex agent message:{_RESET} {len(text)} chars")
                    elif item_type == "dynamicToolCall":
                        log_info(
                            f"{_BLUE}Codex dynamic tool done:{_RESET} "
                            f"tool={item.get('tool', 'unknown')}  "
                            f"status={item.get('status')}  success={item.get('success')}"
                        )
                    continue

                if method == "turn/completed":
                    turn = params.get("turn")
                    if isinstance(turn, dict):
                        completed_turn_id = turn.get("id")
                        if isinstance(completed_turn_id, str) and completed_turn_id:
                            turn_id = completed_turn_id
                        turn_status = str(turn.get("status") or "")
                        turn_error_text = _codex_turn_error_text(turn)
                        if turn_status == "failed" and not turn_error_text:
                            for known_item in reversed(list(known_items.values())):
                                turn_error_text = _codex_failed_item_text(known_item)
                                if turn_error_text:
                                    break
                        log_info(
                            f"{_DIM}Codex turn completed:{_RESET} "
                            f"status={turn_status or 'unknown'}  "
                            f"error={_tail(turn_error_text or '', 300) or '(none)'}  "
                            f"last_host_request={last_host_request or '(none)'}"
                        )
                    break

            try:
                now = asyncio.get_running_loop().time()
                if now - last_edit >= EDIT_INTERVAL:
                    elapsed = int(now - start_time)
                    display = _codex_status_preview(
                        latest_agent_message,
                        current_command,
                        current_command_output,
                        elapsed,
                    )
                    await safe_edit(status_msg, display)
                    last_edit = now
            except Exception as exc:
                turn_status = "failed"
                turn_error_text = str(exc)
                break
    except asyncio.CancelledError:
        cancelled = True
        cancel_reason = s.get("cancel_reason") or cancel_reason
        if thread_id and turn_id:
            await client.interrupt_turn(thread_id, turn_id)
    except Exception as exc:
        log_info(f"{_RED}Codex turn exception:{_RESET} {type(exc).__name__}: {exc}")
        turn_status = "failed"
        turn_error_text = str(exc)

    output = latest_agent_message or current_command_output.strip()
    if turn_status == "failed" and not output:
        if not turn_error_text and last_host_request:
            turn_error_text = f"Codex turn failed after host request: {last_host_request}"
        if turn_error_text:
            output = f"Error: {turn_error_text}"
        else:
            output = "Error: Codex turn failed."
    elif turn_status == "interrupted" and not cancelled:
        cancelled = True
        cancel_reason = s.get("cancel_reason") or cancel_reason

    return output, cancelled, cancel_reason


async def _run_claude_turn(
    prompt: str,
    chat_id: int,
    bot,
    status_msg,
    start_time: float,
    s: dict,
    session: dict[str, Any],
) -> tuple[str, bool, str]:
    """Run a single Claude SDK turn."""
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            SystemMessage,
        )
        from claude_agent_sdk.types import StreamEvent
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("claude_agent_sdk"):
            return (
                "Error: Missing runtime dependency 'claude-agent-sdk'.\n\n"
                "Install it into the Python environment that runs `chatter`, then retry.",
                False,
                "Cancelled.",
            )
        raise

    cwd = s["cwd"]
    stderr_lines: list[str] = []

    def _on_stderr(line: str) -> None:
        stderr_lines.append(line.rstrip())
        log_info(f"{_RED}STDERR:{_RESET} {line}")

    options = ClaudeAgentOptions(
        cwd=cwd,
        can_use_tool=_make_can_use_tool(chat_id, bot),
        include_partial_messages=True,
        setting_sources=["project", "local"],
        add_dirs=[cwd],
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
        },
        stderr=_on_stderr,
    )

    if session.get("session_id"):
        options.resume = session["session_id"]
        log_info(f"Resuming session {_BOLD}{session['session_id']}{_RESET}")
    elif session.get("session_started"):
        options.continue_conversation = True
        log_info("Continuing conversation")
    else:
        log_info("Starting new session")

    streaming_text = ""
    final_output = ""
    result_text = ""
    last_edit = asyncio.get_running_loop().time()
    cancelled = False
    cancel_reason = "Cancelled."

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt, session_id=session.get("session_id") or "default")

            async for message in client.receive_response():
                if s.get("cancelled"):
                    cancelled = True
                    cancel_reason = s.get("cancel_reason") or cancel_reason
                    break

                if isinstance(message, StreamEvent):
                    event = message.event
                    event_type = event.get("type", "")
                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            streaming_text += delta.get("text", "")
                    elif event_type == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            log_info(f"{_BLUE}Tool call:{_RESET} {block.get('name', '?')}")

                elif isinstance(message, AssistantMessage):
                    text_len = 0
                    for block in message.content:
                        if hasattr(block, "text"):
                            final_output += block.text
                            text_len += len(block.text)
                    streaming_text = ""
                    log_info(f"{_YELLOW}AssistantMessage:{_RESET} {text_len} chars")

                elif isinstance(message, ResultMessage):
                    session["session_id"] = message.session_id or session.get("session_id")
                    result_text = message.result or ""
                    session["session_started"] = True
                    log_info(
                        f"{_GREEN}ResultMessage:{_RESET} "
                        f"session={session['session_id']}  result={len(result_text)} chars"
                    )

                elif isinstance(message, SystemMessage):
                    log_info(f"{_DIM}SystemMessage:{_RESET} subtype={message.subtype}")
                    if message.subtype == "init" and hasattr(message, "data"):
                        session_id = (message.data or {}).get("session_id")
                        if session_id:
                            session["session_id"] = session_id
                            session["session_started"] = True

                now = asyncio.get_running_loop().time()
                if now - last_edit >= EDIT_INTERVAL:
                    elapsed = int(now - start_time)
                    display = (final_output + streaming_text) or f"⏳ Thinking… ({elapsed}s)"
                    await safe_edit(status_msg, display)
                    last_edit = now

    except asyncio.CancelledError:
        cancelled = True
        cancel_reason = s.get("cancel_reason") or cancel_reason
    except Exception as exc:
        had_output = bool(final_output or result_text)
        label = "SDK exited after producing output" if had_output else "SDK error"
        color = _YELLOW if had_output else _RED
        log_info(f"{color}{label}:{_RESET} {exc}")
        if not had_output:
            stderr_output = _tail("\n".join(stderr_lines), 1200)
            if stderr_output:
                final_output = f"Error: {exc}\n\nstderr:\n{stderr_output}"
            else:
                final_output = f"Error: {exc}"

    return final_output or result_text, cancelled, cancel_reason


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    s = get_state(chat_id)
    lock: asyncio.Lock = s["lock"]

    if lock.locked():
        _cancel_run(s, reason=_INTERRUPT_REASON)

    async with lock:
        prompt = update.message.text
        log_user(update.effective_user.id, prompt)
        backend = _active_backend(s)
        session = _get_backend_session(s, backend)

        if not await _ensure_agent_auth(update, backend):
            s["running"] = False
            s["run_task"] = None
            return

        s["running"] = True
        s["running_backend"] = backend
        s["cancelled"] = False
        s["cancel_reason"] = None
        s["run_task"] = asyncio.current_task()
        start_time = asyncio.get_running_loop().time()
        status_msg = await update.message.reply_text("⏳ Thinking… (0s)")
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_typing_loop(chat_id, context.bot, stop_typing))

        output = ""
        cancelled = False
        cancel_reason = "Cancelled."

        try:
            if backend == AGENT_CODEX:
                output, cancelled, cancel_reason = await _run_codex_turn(
                    prompt,
                    chat_id,
                    context.bot,
                    status_msg,
                    start_time,
                    s,
                    session,
                )
            else:
                output, cancelled, cancel_reason = await _run_claude_turn(
                    prompt,
                    chat_id,
                    context.bot,
                    status_msg,
                    start_time,
                    s,
                    session,
                )
        finally:
            stop_typing.set()
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

            if s.get("run_task") is asyncio.current_task():
                s["run_task"] = None
            s["agent_process"] = None
            s["running"] = False
            s["running_backend"] = None
            s["cancelled"] = False
            s["cancel_reason"] = None
            log_info(
                f"Done. backend={backend}  session={session.get('session_id')}  out={len(output)}"
            )

        if cancelled and not output:
            if cancel_reason == _INTERRUPT_REASON:
                with suppress(Exception):
                    await status_msg.delete()
                return
            output = cancel_reason
        if output and _looks_like_error_text(output):
            _log_output_preview("User-visible error", output, color=_RED)
        await send_final(status_msg, update.message, output)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_bot(config: BotConfig) -> None:
    global _config
    repo_root = str(Path(config.repo_path).resolve())
    _config = BotConfig(
        bot_token=config.bot_token,
        allowed_user_id=config.allowed_user_id,
        repo_path=repo_root,
        repo_name=config.repo_name,
        agent_backend=config.agent_backend,
    )

    # Claude can be selected later via /agent even when it is not the repo default.
    # Keep the extended timeout configured whenever the bot is running.
    os.environ.setdefault(
        "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT",
        str(APPROVAL_TIMEOUT * 1000 + 60_000),
    )

    app = Application.builder().token(_config.bot_token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("agent", agent_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CallbackQueryHandler(_approval_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log_startup(_config.allowed_user_id, _config.repo_path, _config.agent_backend)
    app.run_polling(drop_pending_updates=True)
