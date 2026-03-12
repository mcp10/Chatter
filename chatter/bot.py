#!/usr/bin/env python3
"""Telegram bot that bridges messages to a local Claude CLI agent."""

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

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    StreamEvent,
)

# ---------------------------------------------------------------------------
# Runtime config (injected by run_bot before the event loop starts)
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    bot_token: str
    allowed_user_id: int
    repo_path: str
    repo_name: str

_config: BotConfig | None = None


# ---------------------------------------------------------------------------
# Terminal logging helpers
# ---------------------------------------------------------------------------

# Enable ANSI colors on Windows (no-op on Unix/macOS and modern Windows Terminal)
colorama.just_fix_windows_console()

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


def log_startup(user_id: int, repo: str) -> None:
    print(f"\n{_GREEN}{_BOLD}Bot started.{_RESET}  user={_BOLD}{user_id}{_RESET}  repo={repo}\n")


EDIT_INTERVAL = 2.0       # seconds between live message edits
MAX_MSG_LEN = 3500        # headroom for HTML escaping overhead (Telegram limit is 4096)
APPROVAL_TIMEOUT = 600    # seconds to wait for user approval before auto-deny (10 min)
MAX_APPROVAL_PREVIEW = 1200  # keep approval prompts comfortably under Telegram limits

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


def get_state(chat_id: int) -> dict:
    if chat_id not in _state:
        _state[chat_id] = {
            "cwd": _config.repo_path,
            "lock": asyncio.Lock(),
            "running": False,
            "run_task": None,
            "session_id": None,
            "session_started": False,
            "cancelled": False,
            "cancel_reason": None,
            "pending_approvals": {},   # approval_id -> asyncio.Future
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
        await reply_to.reply_text("(no output)")
        return
    chunks = [text[i:i + MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)]
    for chunk in chunks:
        await reply_to.reply_text(md_to_html(chunk), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Tool approval helpers
# ---------------------------------------------------------------------------

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


def _make_can_use_tool(chat_id: int, bot):
    """Factory that creates a can_use_tool callback bound to a specific chat."""

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

        # Request approval via Telegram inline keyboard
        approval_id = uuid.uuid4().hex[:8]
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        s["pending_approvals"][approval_id] = future

        text = _format_tool_request(tool_name, input_data)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Approve", callback_data=f"{_CB_APPROVE}:{approval_id}"),
                InlineKeyboardButton("Deny",    callback_data=f"{_CB_DENY}:{approval_id}"),
            ]
        ])

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as e:
            log_info(f"{_RED}Failed to send approval message:{_RESET} {e}")
            if not future.done():
                future.cancel()
            s["pending_approvals"].pop(approval_id, None)
            return PermissionResultDeny(message=f"Could not send approval request: {e}")

        log_info(f"{_YELLOW}Approval requested:{_RESET} {tool_name} [{approval_id}]")

        # Wait for the user to tap Approve / Deny.
        # Use a plain loop + short sleeps instead of asyncio.wait_for to avoid
        # creating internal asyncio Tasks that conflict with anyio's TaskGroup.
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
                    # Complete the future to avoid leaving it pending on timeout.
                    if not future.done():
                        future.set_result(False)
                    return PermissionResultDeny(message="Approval timed out")
                await asyncio.sleep(min(0.25, remaining))

            approved = future.result()
        finally:
            s["pending_approvals"].pop(approval_id, None)

        if approved:
            log_info(f"{_GREEN}Approved:{_RESET} {tool_name} [{approval_id}]")
            return PermissionResultAllow(updated_input=input_data)
        else:
            log_info(f"{_RED}Denied:{_RESET} {tool_name} [{approval_id}]")
            return PermissionResultDeny(message=f"User denied {tool_name}")

    return can_use_tool


def _cancel_run(s: dict, *, reason: str) -> bool:
    """Cancel the active run task and unblock any pending approvals."""
    had_run = bool(s.get("running"))
    s["cancelled"] = True
    s["cancel_reason"] = reason

    for future in s["pending_approvals"].values():
        if not future.done():
            future.set_result(False)
    s["pending_approvals"].clear()

    run_task = s.get("run_task")
    if run_task and not run_task.done():
        run_task.cancel()
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
        approved = prefix == _CB_APPROVE
        future = s["pending_approvals"].get(approval_id)
        if future and not future.done():
            future.set_result(approved)
            label = "APPROVED" if approved else "DENIED"
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
    await update.message.reply_text(
        f"Claude agent bridge ready.\n"
        f"Repo: {_config.repo_name} ({s['cwd']})\n\n"
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
    s["session_id"] = None
    s["session_started"] = False
    await update.message.reply_text("Session reset. Next prompt will start a new conversation.")


# ---------------------------------------------------------------------------
# Main message handler — runs Claude via SDK and streams output
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    s = get_state(chat_id)
    lock: asyncio.Lock = s["lock"]

    # Keep at most one active run per chat: interrupt the previous one if needed.
    if lock.locked():
        _cancel_run(s, reason=_INTERRUPT_REASON)

    async with lock:
        prompt = update.message.text
        cwd = s["cwd"]
        log_user(update.effective_user.id, prompt)

        s["running"] = True
        s["cancelled"] = False
        s["cancel_reason"] = None
        s["run_task"] = asyncio.current_task()
        start_time = asyncio.get_running_loop().time()
        status_msg = await update.message.reply_text("⏳ Thinking… (0s)")
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(
            _typing_loop(chat_id, context.bot, stop_typing)
        )

        stderr_lines: list[str] = []

        def _on_stderr(line: str) -> None:
            stderr_lines.append(line.rstrip())
            log_info(f"{_RED}STDERR:{_RESET} {line}")

        # Build SDK options
        options = ClaudeAgentOptions(
            cwd=cwd,
            can_use_tool=_make_can_use_tool(chat_id, context.bot),
            include_partial_messages=True,
            # Ignore global user settings to keep the runtime constrained
            # to this repo's own Claude settings.
            setting_sources=["project", "local"],
            add_dirs=[cwd],
            sandbox={
                "enabled": True,
                "autoAllowBashIfSandboxed": False,
                "allowUnsandboxedCommands": False,
            },
            stderr=_on_stderr,
        )

        if s.get("session_id"):
            options.resume = s["session_id"]
            log_info(f"Resuming session {_BOLD}{s['session_id']}{_RESET}")
        elif s.get("session_started"):
            options.continue_conversation = True
            log_info("Continuing conversation")
        else:
            log_info("Starting new session")

        streaming_text = ""   # accumulated from StreamEvents (real-time)
        final_output = ""     # accumulated from AssistantMessages (authoritative)
        result_text = ""
        last_edit = asyncio.get_running_loop().time()
        cancelled = False
        cancel_reason = "Cancelled."

        try:
            # Use the standard one-shot prompt path here. Streaming input is only
            # needed for multi-message/custom-tool flows, and it is known to hit
            # SDK transport shutdown bugs for single-prompt sessions.
            async for message in query(prompt=prompt, options=options):
                # Check for cancellation
                if s.get("cancelled"):
                    cancelled = True
                    cancel_reason = s.get("cancel_reason") or cancel_reason
                    break

                if isinstance(message, StreamEvent):
                    # Real-time text deltas for live preview
                    event = message.event
                    etype = event.get("type", "")
                    if etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            streaming_text += delta.get("text", "")
                    elif etype == "content_block_start":
                        block = event.get("content_block", {})
                        btype = block.get("type", "")
                        if btype == "tool_use":
                            log_info(f"{_BLUE}Tool call:{_RESET} {block.get('name', '?')}")

                elif isinstance(message, AssistantMessage):
                    # Authoritative full text — replaces streaming accumulator
                    text_len = 0
                    for block in message.content:
                        if hasattr(block, "text"):
                            final_output += block.text
                            text_len += len(block.text)
                    streaming_text = ""
                    log_info(f"{_YELLOW}AssistantMessage:{_RESET} {text_len} chars")

                elif isinstance(message, ResultMessage):
                    s["session_id"] = message.session_id or s.get("session_id")
                    result_text = message.result or ""
                    s["session_started"] = True
                    log_info(f"{_GREEN}ResultMessage:{_RESET} session={s['session_id']}  result={len(result_text)} chars")

                elif isinstance(message, SystemMessage):
                    log_info(f"{_DIM}SystemMessage:{_RESET} subtype={message.subtype}")
                    if message.subtype == "init" and hasattr(message, "data"):
                        sid = (message.data or {}).get("session_id")
                        if sid:
                            s["session_id"] = sid

                # Live-edit the status message
                now = asyncio.get_running_loop().time()
                if now - last_edit >= EDIT_INTERVAL:
                    elapsed = int(now - start_time)
                    display = (final_output + streaming_text) or f"⏳ Thinking… ({elapsed}s)"
                    await safe_edit(status_msg, display)
                    last_edit = now

        except asyncio.CancelledError:
            cancelled = True
            cancel_reason = s.get("cancel_reason") or cancel_reason
        except Exception as e:
            had_output = bool(final_output or result_text)
            label = "SDK exited after producing output" if had_output else "SDK error"
            color = _YELLOW if had_output else _RED
            log_info(f"{color}{label}:{_RESET} {e}")
            if not had_output:
                stderr_output = _tail("\n".join(stderr_lines), 1200)
                if stderr_output:
                    final_output = f"Error: {e}\n\nstderr:\n{stderr_output}"
                else:
                    final_output = f"Error: {e}"
        finally:
            stop_typing.set()
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

            if s.get("run_task") is asyncio.current_task():
                s["run_task"] = None
            s["running"] = False
            s["cancelled"] = False
            s["cancel_reason"] = None
            log_info(f"Done. session={s.get('session_id')}  out={len(final_output)}  result={len(result_text)}")

        output = final_output or result_text
        if cancelled and not output:
            if cancel_reason == _INTERRUPT_REASON:
                # Silently remove the Thinking indicator — the new query will
                # send its own, so the user never sees two Thinking bubbles.
                with suppress(Exception):
                    await status_msg.delete()
                return
            output = cancel_reason
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
    )

    # The SDK closes stdin after this timeout (ms) if it hasn't received a result.
    # Default 60 s is too short when waiting for human approval on Telegram.
    os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", str(APPROVAL_TIMEOUT * 1000 + 60_000))

    app = Application.builder().token(_config.bot_token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CallbackQueryHandler(_approval_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log_startup(_config.allowed_user_id, _config.repo_path)
    app.run_polling(drop_pending_updates=True)
