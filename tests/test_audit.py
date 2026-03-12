"""Tests for the audit logging module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chatter.audit import AuditLogger, _redact_dict, _redact_token

# ---------------------------------------------------------------------------
# Token redaction
# ---------------------------------------------------------------------------

class TestRedactToken:
    def test_masks_bot_token(self):
        result = _redact_token("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
        assert result.startswith("***")
        assert result.endswith("w11")
        assert "123456" not in result

    def test_leaves_non_token_strings(self):
        assert _redact_token("hello world") == "hello world"
        assert _redact_token("short") == "short"

    def test_masks_token_in_context(self):
        text = "token is 123456:ABCDEFghijklmnopqrstuv and more"
        result = _redact_token(text)
        assert "123456" not in result
        assert "and more" in result


# ---------------------------------------------------------------------------
# Dict redaction
# ---------------------------------------------------------------------------

class TestRedactDict:
    def test_truncates_long_strings(self):
        data = {"key": "x" * 300}
        result = _redact_dict(data)
        assert len(result["key"]) < 300
        assert "300 chars" in result["key"]

    def test_preserves_short_strings(self):
        data = {"key": "short"}
        assert _redact_dict(data)["key"] == "short"

    def test_redacts_nested_tokens(self):
        data = {"config": {"token": "123456:ABCDEFghijklmnopqrstuv"}}
        result = _redact_dict(data)
        assert "123456" not in str(result)

    def test_handles_lists(self):
        data = {"items": ["hello", {"nested": "value"}]}
        result = _redact_dict(data)
        assert result["items"][0] == "hello"
        assert result["items"][1]["nested"] == "value"

    def test_preserves_non_string_values(self):
        data = {"count": 42, "flag": True, "empty": None}
        result = _redact_dict(data)
        assert result["count"] == 42
        assert result["flag"] is True
        assert result["empty"] is None


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------

class TestAuditLogger:
    @pytest.fixture
    def log_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "logs"
        return d  # AuditLogger creates it

    @pytest.fixture
    def logger(self, log_dir: Path) -> AuditLogger:
        return AuditLogger(log_dir=log_dir)

    def _read_logs(self, log_dir: Path) -> list[dict]:
        log_file = log_dir / "audit.jsonl"
        if not log_file.exists():
            return []
        lines = log_file.read_text().strip().splitlines()
        return [json.loads(line) for line in lines]

    def test_creates_log_directory(self, log_dir: Path, logger: AuditLogger):
        assert log_dir.exists()
        assert (log_dir.stat().st_mode & 0o777) == 0o700

    def test_log_message_standard(self, log_dir: Path, logger: AuditLogger):
        logger.log_message(chat_id=1, user_id=42, text="Hello agent")
        logs = self._read_logs(log_dir)
        assert len(logs) == 1
        assert logs[0]["event"] == "user_message"
        assert logs[0]["user_id"] == 42
        # Standard mode: no text content, only length
        assert "text" not in logs[0]
        assert logs[0]["text_len"] == len("Hello agent")

    def test_log_tool_request(self, log_dir: Path, logger: AuditLogger):
        logger.log_tool_request(
            chat_id=1,
            tool_name="Bash",
            input_data={"command": "ls -la"},
        )
        logs = self._read_logs(log_dir)
        assert len(logs) == 1
        assert logs[0]["event"] == "tool_request"
        assert logs[0]["tool"] == "Bash"

    def test_log_approval(self, log_dir: Path, logger: AuditLogger):
        logger.log_approval(chat_id=1, tool_name="Edit", decision="approved")
        logs = self._read_logs(log_dir)
        assert logs[0]["event"] == "approval_decision"
        assert logs[0]["decision"] == "approved"

    def test_log_scope_violation(self, log_dir: Path, logger: AuditLogger):
        logger.log_scope_violation(
            chat_id=1,
            tool_name="Bash",
            reason="Path outside repo",
        )
        logs = self._read_logs(log_dir)
        assert logs[0]["event"] == "scope_violation"
        assert "outside" in logs[0]["reason"]

    def test_log_session(self, log_dir: Path, logger: AuditLogger):
        logger.log_session(chat_id=1, event="start", backend="claude")
        logs = self._read_logs(log_dir)
        assert logs[0]["event"] == "session"
        assert logs[0]["session_event"] == "start"
        assert logs[0]["backend"] == "claude"

    def test_log_error(self, log_dir: Path, logger: AuditLogger):
        logger.log_error(chat_id=1, error="Something went wrong")
        logs = self._read_logs(log_dir)
        assert logs[0]["event"] == "error"

    def test_log_auth_failure(self, log_dir: Path, logger: AuditLogger):
        logger.log_auth_failure(user_id=99999, chat_type="group")
        logs = self._read_logs(log_dir)
        assert logs[0]["event"] == "auth_failure"
        assert logs[0]["user_id"] == 99999

    def test_log_startup(self, log_dir: Path, logger: AuditLogger):
        logger.log_startup(user_id=42, repo="/home/user/repo", backend="claude")
        logs = self._read_logs(log_dir)
        assert logs[0]["event"] == "bot_startup"

    def test_multiple_events(self, log_dir: Path, logger: AuditLogger):
        logger.log_startup(user_id=1, repo="/r", backend="claude")
        logger.log_message(chat_id=1, user_id=1, text="hi")
        logger.log_tool_request(chat_id=1, tool_name="Read", input_data={})
        logger.log_approval(chat_id=1, tool_name="Read", decision="auto_approved")
        logs = self._read_logs(log_dir)
        assert len(logs) == 4

    def test_all_records_have_timestamp(self, log_dir: Path, logger: AuditLogger):
        logger.log_message(chat_id=1, user_id=1, text="test")
        logger.log_error(chat_id=1, error="oops")
        logs = self._read_logs(log_dir)
        for log in logs:
            assert "ts" in log
            assert isinstance(log["ts"], float)
