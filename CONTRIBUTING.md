# Contributing to Chatter

Thank you for your interest in contributing to Chatter! This guide will help you get started.

## Development Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/maximilienpicquet/Chatter.git
   cd Chatter
   ```

2. **Create a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install with dev dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

## Running Checks

Before submitting a PR, make sure all checks pass:

```bash
# Run all checks
make check

# Or run checks individually
make guardrails
make lint
make typecheck
make test
make audit
```

Equivalent direct commands:

```bash
# Run tests
pytest tests/ -v

# Run linter
ruff check chatter/ tests/

# Run type checker
mypy chatter/ --ignore-missing-imports

# Run security audit
pip-audit
```

## Code Style

- **Linter**: We use [ruff](https://docs.astral.sh/ruff/) for linting and import sorting.
- **Type hints**: We use [mypy](https://mypy-lang.org/) for static type checking (permissive mode).
- **Line length**: 120 characters max.
- **Python version**: Target Python 3.10+.

Configuration is in `ruff.toml` and `pyproject.toml`.

## Pull Request Process

1. **Fork** the repository and create a feature branch.
2. **Write tests** for any new functionality (especially security-critical code).
3. **Ensure all checks pass** (tests, lint, type check).
4. **Write a clear PR description** explaining the what, why, and how.
5. **One feature per PR** — keep changes focused and reviewable.

## Project Structure

```
chatter/
  __init__.py          # version metadata
  bot.py               # core bot logic, sandboxing, approval workflow
  cli.py               # CLI entry points (init, start, notify)
  config.py            # config management (~/.chatter/config.yaml)
  audit.py             # structured audit logging
  agent.py             # backend abstractions (Claude/Codex)
  notify.py            # Telegram startup notifications
  claude_auth.py       # Claude CLI auth checker
  codex_auth.py        # Codex CLI auth checker
  codex_app_server.py  # JSON-RPC client for Codex
tests/
  conftest.py          # shared fixtures
  test_auth.py         # authentication tests
  test_bash_sandbox.py # bash command sandboxing tests
  test_path_validation.py  # path validation tests
  test_config.py       # config loading/saving tests
  test_audit.py        # audit logging tests
```

## Security Considerations

If your contribution touches security-critical code (path validation, auth, sandboxing):

- **Add comprehensive tests** including edge cases.
- **Consider bypass scenarios** — how could an attacker circumvent the check?
- **Document the threat model** impact in your PR description.
- **See [SECURITY.md](SECURITY.md)** for the current threat model.

## Questions?

Open a GitHub issue for questions about contributing.
