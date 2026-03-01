#!/usr/bin/env python3
"""Telegram bot that bridges messages to a local Claude CLI agent."""

from __future__ import annotations

import asyncio
import html as _html
import json
import re
from dataclasses import dataclass
from datetime import datetime

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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


def log_stdout(line: str) -> None:
    print(f"{_DIM}{_ts()}{_RESET}  {_DIM}[stdout]{_RESET} {line}")


def log_stderr(text: str) -> None:
    for line in text.splitlines():
        print(f"{_DIM}{_ts()}{_RESET}  {_YELLOW}[stderr]{_RESET} {line}")


def log_exit(code: int, session_id: str | None, out_len: int, res_len: int) -> None:
    color = _GREEN if code == 0 else _RED
    print(
        f"{_DIM}{_ts()}{_RESET}  {color}{_BOLD}[exit {code}]{_RESET}"
        f"  session={session_id}  out={out_len}  result={res_len}"
    )


def log_startup(user_id: int, repo: str) -> None:
    print(f"\n{_GREEN}{_BOLD}Bot started.{_RESET}  user={_BOLD}{user_id}{_RESET}  repo={repo}\n")


EDIT_INTERVAL = 2.0       # seconds between live message edits
MAX_MSG_LEN = 3500        # headroom for HTML escaping overhead (Telegram limit is 4096)
SUBPROCESS_TIMEOUT = 300  # seconds of silence before killing a hung claude process


# ---------------------------------------------------------------------------
# Per-chat state
# ---------------------------------------------------------------------------

_state: dict = {}


def get_state(chat_id: int) -> dict:
    if chat_id not in _state:
        _state[chat_id] = {
            "cwd": _config.repo_path,
            "proc": None,
            "session_id": None,
            "session_started": False,
        }
    return _state[chat_id]


def is_allowed(update: Update) -> bool:
    return update.effective_user.id == _config.allowed_user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def trim(text: str) -> str:
    """Keep last MAX_MSG_LEN chars so the message always shows the latest output."""
    if len(text) > MAX_MSG_LEN:
        text = "…" + text[-MAX_MSG_LEN:]
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

        # Split on inline code spans.
        inline_segments = re.split(r"(`[^`]+`)", seg)
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
    proc = s.get("proc")
    if proc and proc.returncode is None:
        proc.terminate()
        s["proc"] = None
        await update.message.reply_text("Agent stopped.")
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
# Main message handler — runs Claude and streams output
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    s = get_state(update.effective_chat.id)

    # Kill any existing process before starting a new one
    if s.get("proc") and s["proc"].returncode is None:
        s["proc"].terminate()
        await asyncio.sleep(0.2)

    prompt = update.message.text
    cwd = s["cwd"]
    log_user(update.effective_user.id, prompt)

    start_time = asyncio.get_running_loop().time()
    status_msg = await update.message.reply_text("⏳ Thinking… (0s)")
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        _typing_loop(update.effective_chat.id, context.bot, stop_typing)
    )

    cmd = ["claude", "--print", "--verbose", "--output-format", "stream-json", "--dangerously-skip-permissions"]
    if s.get("session_id"):
        cmd += ["--resume", s["session_id"]]
    elif s.get("session_started"):
        cmd.append("--continue")
    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=10 * 1024 * 1024,  # 10 MB — default 64 KB is too small for Claude's JSON lines
    )
    s["proc"] = proc

    output = ""
    result_text = ""
    last_edit = asyncio.get_running_loop().time()
    timed_out = False

    while True:
        try:
            raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=SUBPROCESS_TIMEOUT)
        except asyncio.TimeoutError:
            log_info(f"Claude produced no output for {SUBPROCESS_TIMEOUT}s — terminating.")
            proc.terminate()
            timed_out = True
            break
        if not raw_line:  # EOF
            break

        line = raw_line.decode(errors="replace").strip()
        log_stdout(line)
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            output += line + "\n"
        else:
            event_type = event.get("type")
            if event_type == "system" and event.get("subtype") == "init":
                s["session_id"] = event.get("session_id") or s.get("session_id")
            elif event_type == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        output += block["text"]
            elif event_type == "result":
                s["session_id"] = event.get("session_id") or s.get("session_id")
                result_text = event.get("result", "")

        now = asyncio.get_running_loop().time()
        if now - last_edit >= EDIT_INTERVAL:
            elapsed = int(now - start_time)
            display = output if output else f"⏳ Thinking… ({elapsed}s)"
            await safe_edit(status_msg, display)
            last_edit = now

    stderr_out = await proc.stderr.read()
    if stderr_out:
        log_stderr(stderr_out.decode(errors="replace"))

    await proc.wait()
    stop_typing.set()
    typing_task.cancel()
    s["proc"] = None
    if proc.returncode == 0:
        s["session_started"] = True
    log_exit(proc.returncode, s.get("session_id"), len(output), len(result_text))

    if timed_out:
        await send_final(status_msg, update.message, output or f"⏱ No response after {SUBPROCESS_TIMEOUT}s.")
        return

    if not output:
        output = result_text

    await send_final(status_msg, update.message, output)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_bot(config: BotConfig) -> None:
    global _config
    _config = config

    app = Application.builder().token(config.bot_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log_startup(config.allowed_user_id, config.repo_path)
    app.run_polling(drop_pending_updates=True)
