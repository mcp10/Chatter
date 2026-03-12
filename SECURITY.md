# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.5.x   | :white_check_mark: |
| < 0.5   | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability in Chatter, please report it responsibly:

1. **Do NOT open a public GitHub issue** for security vulnerabilities.
2. Use [GitHub Security Advisories](https://github.com/maximilienpicquet/Chatter/security/advisories/new) to report privately.
3. Alternatively, email the maintainer directly.

You can expect:
- **Acknowledgement** within 48 hours
- **Initial assessment** within 1 week
- **Fix or mitigation** within 2 weeks for critical issues

## Threat Model

### Trust Boundaries

```
Telegram Cloud  <-->  Chatter Bot  <-->  Claude/Codex CLI  <-->  Local Filesystem
    (API)              (Python)           (subprocess)            (repo-scoped)
```

### Authentication
- **Single-user design**: Only one Telegram user ID is permitted per installation.
- **Private chat enforcement**: Bot ignores all group/channel messages.
- **Telegram trust**: Authentication relies on Telegram's user ID system.
  Telegram user IDs cannot be spoofed within the Telegram API, but the bot trusts
  Telegram's infrastructure to deliver accurate user information.

### Sandboxing
- **Repository-scoped**: All file operations are restricted to the registered repo directory.
- **Path validation**: Absolute paths, `..` traversals, and `~` expansions are checked against the repo boundary.
- **Bash command inspection**: Shell commands are parsed and validated for path escapes.
- **Claude SDK sandbox**: Runs with `sandbox.enabled=True` and `autoAllowBashIfSandboxed=False`.
- **Tool approval**: Destructive tools (Bash, Edit, Write) require explicit Telegram approval.
  Read-only tools (Read, Glob, Grep, WebSearch, WebFetch) are auto-approved.

### Configuration Security
- **Centralized config**: All secrets stored at `~/.chatter/config.yaml` (never in repo directories).
- **File permissions**: Config file automatically set to `chmod 600`, directory to `chmod 700`.
- **Permission warnings**: Bot warns at startup if config file is readable by group/others.

### Audit Logging
- **Persistent logs**: All tool invocations, approval decisions, and auth failures are logged to `~/.chatter/logs/audit.jsonl`.
- **PII redaction**: Bot tokens are masked in logs. User prompt content is excluded by default.
- **Rotation**: Logs rotate at 5 MB with 10 backup files.

### Known Limitations
- **Regex-based bash validation**: The bash command validator uses pattern matching, not a full shell parser. Exotic shell syntax may bypass validation. The tool approval workflow provides a second layer of defense.
- **Single-factor auth**: Telegram user ID is the only authentication factor. If a Telegram account is compromised, the attacker gains bot access.
- **No rate limiting**: There is no built-in rate limiting on approval requests or message handling.
- **Local execution**: The Claude/Codex agent runs with the permissions of the local user. Repository sandboxing limits the scope, but a compromised agent with an approved Bash tool could potentially escape via techniques not caught by the regex validator.

## Security Best Practices

1. **Keep your Telegram account secure** (2FA enabled).
2. **Use a dedicated bot token** per repository.
3. **Review tool approval requests carefully** before approving.
4. **Monitor audit logs** at `~/.chatter/logs/audit.jsonl`.
5. **Keep dependencies updated**: Run `pip-audit` regularly.
6. **Verify config permissions**: `ls -la ~/.chatter/config.yaml` should show `-rw-------`.
