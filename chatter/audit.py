"""Structured audit logging for Chatter.

Provides persistent, JSON-lines audit logs alongside the existing console output.
Every tool invocation, approval decision, and agent action is recorded.

Log files:
    ~/.chatter/logs/audit.jsonl       (current)
    ~/.chatter/logs/audit.jsonl.1-10  (rotated backups)

Privacy:
    By default, user prompt content is NOT logged (only metadata).
    Set env var CHATTER_AUDIT_LEVEL=verbose to include prompt text.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_LOG_DIR = Path.home() / ".chatter" / "logs"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per log file
_BACKUP_COUNT = 10
_TOKEN_MASK_RE = re.compile(r"\d+:[A-Za-z0-9_-]{20,}")


# ---------------------------------------------------------------------------
# PII / secret redaction
# ---------------------------------------------------------------------------

def _redact_token(value: str) -> str:
    """Mask bot tokens, keeping only the last 4 characters."""
    return _TOKEN_MASK_RE.sub(lambda m: "***" + m.group()[-4:], value)


def _redact_dict(data: dict[str, Any], *, max_str_len: int = 200) -> dict[str, Any]:
    """Deep-copy a dict, redacting long strings and bot tokens."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            value = _redact_token(value)
            if len(value) > max_str_len:
                value = value[:max_str_len] + f"... [{len(value)} chars]"
        elif isinstance(value, dict):
            value = _redact_dict(value, max_str_len=max_str_len)
        elif isinstance(value, list):
            value = [
                _redact_dict(v, max_str_len=max_str_len) if isinstance(v, dict)
                else _redact_token(v) if isinstance(v, str)
                else v
                for v in value
            ]
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------

class AuditLogger:
    """Append-only JSON-lines audit logger with rotation and PII redaction."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self._log_dir = log_dir or _DEFAULT_LOG_DIR
        self._verbose = os.environ.get("CHATTER_AUDIT_LEVEL", "").lower() == "verbose"
        self._logger = self._setup_logger()

    # -- setup --------------------------------------------------------------

    def _setup_logger(self) -> logging.Logger:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._log_dir, 0o700)

        logger = logging.getLogger("chatter.audit")
        logger.setLevel(logging.INFO)
        # Avoid duplicate handlers on re-init
        logger.handlers.clear()

        handler = RotatingFileHandler(
            self._log_dir / "audit.jsonl",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        return logger

    # -- core emit ----------------------------------------------------------

    def _emit(self, event: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": time.time(),
            "event": event,
        }
        record.update(fields)
        self._logger.info(json.dumps(record, default=str))

    # -- public API ---------------------------------------------------------

    def log_message(self, chat_id: int, user_id: int, text: str) -> None:
        """Log an incoming user message."""
        detail: dict[str, Any] = {"chat_id": chat_id, "user_id": user_id}
        if self._verbose:
            detail["text"] = text[:500]  # cap even in verbose mode
        else:
            detail["text_len"] = len(text)
        self._emit("user_message", **detail)

    def log_tool_request(
        self,
        chat_id: int,
        tool_name: str,
        input_data: dict[str, Any],
    ) -> None:
        """Log a tool invocation request from the agent."""
        safe_input = _redact_dict(input_data) if self._verbose else {"keys": list(input_data.keys())}
        self._emit(
            "tool_request",
            chat_id=chat_id,
            tool=tool_name,
            input=safe_input,
        )

    def log_approval(
        self,
        chat_id: int,
        tool_name: str,
        decision: str,
    ) -> None:
        """Log an approval decision (approved / denied / timeout)."""
        self._emit(
            "approval_decision",
            chat_id=chat_id,
            tool=tool_name,
            decision=decision,
        )

    def log_scope_violation(
        self,
        chat_id: int,
        tool_name: str,
        reason: str,
    ) -> None:
        """Log a repo-scope violation (blocked tool request)."""
        self._emit(
            "scope_violation",
            chat_id=chat_id,
            tool=tool_name,
            reason=reason,
        )

    def log_session(
        self,
        chat_id: int,
        event: str,
        backend: str | None = None,
    ) -> None:
        """Log a session lifecycle event (start / end / resume)."""
        fields: dict[str, Any] = {"chat_id": chat_id, "session_event": event}
        if backend:
            fields["backend"] = backend
        self._emit("session", **fields)

    def log_error(self, chat_id: int, error: str) -> None:
        """Log an error that occurred during processing."""
        self._emit(
            "error",
            chat_id=chat_id,
            error=error[:500],
        )

    def log_auth_failure(self, user_id: int, chat_type: str) -> None:
        """Log a failed authentication attempt."""
        self._emit(
            "auth_failure",
            user_id=user_id,
            chat_type=chat_type,
        )

    def log_startup(self, user_id: int, repo: str, backend: str) -> None:
        """Log bot startup."""
        self._emit(
            "bot_startup",
            user_id=user_id,
            repo=repo,
            backend=backend,
        )
