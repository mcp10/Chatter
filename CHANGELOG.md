# Changelog

All notable changes to Chatter will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-03-12

### Added
- **Test suite**: 111 tests covering path validation, bash sandboxing, authentication, config management, and audit logging.
- **Audit logging**: Persistent JSON-lines audit logs at `~/.chatter/logs/audit.jsonl` with rotation and PII redaction.
- **Config permissions**: Automatic `chmod 600` on config file, `chmod 700` on config directory. Warning on startup if permissions are too open.
- **CI pipeline**: Added lint (ruff), type check (mypy), test (pytest), and security scan (pip-audit) jobs to GitHub Actions.
- **Open-source docs**: LICENSE (MIT), SECURITY.md (threat model + vulnerability reporting), CONTRIBUTING.md, CHANGELOG.md.

### Fixed
- **Regex bug in bash validator**: `_EMBEDDED_ABS_PATH_RE` used `\\s` (literal backslash + s) instead of `\s` (whitespace) in character classes, causing paths containing the letter 's' to be incorrectly truncated during validation.

### Changed
- Imports sorted with isort/ruff across all source files.

## [0.5.0] - 2025

### Added
- Dual backend support: Claude Code and Codex.
- Tool approval workflow via Telegram inline buttons.
- Repository-scoped file and bash access control.
- Centralized config at `~/.chatter/config.yaml`.
- `chatter init` / `chatter start` / `chatter notify` CLI commands.
- Session continuity with `--resume` support.
