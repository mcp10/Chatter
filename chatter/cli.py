"""CLI entry points for Chatter."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from .agent import AGENT_CLAUDE, AGENT_CODEX, SUPPORTED_AGENT_BACKENDS, agent_label
from .claude_auth import format_claude_auth_error, get_claude_auth_status
from .config import GLOBAL_CONFIG_FILE, ChatterConfig, RepoEntry
from .codex_auth import format_codex_auth_error, get_codex_auth_status
from .notify import send_startup_notification

MIN_PYTHON = (3, 10)
REPO_URL = "https://github.com/mcp10/Chatter.git"


def _ensure_supported_python() -> None:
    """Fail fast with a clear message when Python is too old."""
    if sys.version_info >= MIN_PYTHON:
        return
    current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    required = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    raise click.ClickException(
        f"Chatter requires Python {required}+ (current: {current}).\n"
        f"Interpreter: {sys.executable}\n"
        "Reinstall with a supported interpreter, for example:\n"
        f'  python -m pip install --upgrade --force-reinstall "git+{REPO_URL}"\n'
        "On Windows, you can use:\n"
        f'  py -3.10 -m pip install --upgrade --force-reinstall "git+{REPO_URL}"'
    )


def _import_bot_runtime():
    """Import bot runtime lazily so init/notify still work when bot deps are missing."""
    from .bot import BotConfig, run_bot
    return BotConfig, run_bot


def _load_config() -> ChatterConfig:
    """Load the central config, raising ClickException on failure."""
    try:
        return ChatterConfig.load()
    except (FileNotFoundError, ValueError) as e:
        raise click.ClickException(str(e))


def _suggest_agent_backend() -> str:
    """Pick a sensible init default based on available local logins."""
    if get_codex_auth_status(timeout_seconds=1.5).ok:
        return AGENT_CODEX
    if get_claude_auth_status(timeout_seconds=1.5).ok:
        return AGENT_CLAUDE
    return AGENT_CODEX


def _ensure_agent_auth(agent_backend: str) -> None:
    """Fail fast when the selected local agent CLI is unavailable or logged out."""
    if agent_backend == AGENT_CODEX:
        codex_auth_status = get_codex_auth_status()
        if not codex_auth_status.ok:
            raise click.ClickException(format_codex_auth_error(codex_auth_status))
        return

    claude_auth_status = get_claude_auth_status()
    if not claude_auth_status.ok:
        raise click.ClickException(format_claude_auth_error(claude_auth_status))


def _load_repo_config() -> tuple[ChatterConfig, str, RepoEntry]:
    """Load config and find the repo matching cwd."""
    cfg = _load_config()
    try:
        name, repo = cfg.find_repo_by_cwd()
    except LookupError as e:
        raise click.ClickException(str(e))
    return cfg, name, repo


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Chatter — Telegram bot bridging messages to a local Codex or Claude agent."""
    _ensure_supported_python()
    if ctx.invoked_subcommand is None:
        _run_start()


@main.command()
def init() -> None:
    """Register the current repository with Chatter."""
    # --- Global config (allowed_user_id) ---
    if GLOBAL_CONFIG_FILE.exists():
        cfg = ChatterConfig.load()
        click.echo(f"Using existing global user ID: {cfg.allowed_user_id}")
    else:
        user_id = click.prompt("Enter your Telegram user ID (allowed_user_id)", type=int)
        cfg = ChatterConfig(allowed_user_id=user_id, repos={})
        cfg.save()
        click.echo(f"Saved to {GLOBAL_CONFIG_FILE}")

    # --- Check if cwd is already registered ---
    cwd = str(Path.cwd().resolve())
    cwd_norm = os.path.normcase(cwd)
    existing_name = None
    for name, entry in cfg.repos.items():
        if os.path.normcase(str(Path(entry.path).resolve())) == cwd_norm:
            existing_name = name
            break

    if existing_name:
        click.confirm(
            f"This directory is already registered as '{existing_name}'. Overwrite?",
            abort=True,
        )
        del cfg.repos[existing_name]

    # --- Register this repo ---
    bot_token = click.prompt("Enter the Telegram bot token for this repo (from BotFather)")
    default_name = Path.cwd().name
    repo_name = click.prompt("Repo name", default=default_name)
    agent_backend = click.prompt(
        "Agent backend",
        type=click.Choice(SUPPORTED_AGENT_BACKENDS, case_sensitive=False),
        default=_suggest_agent_backend(),
        show_choices=True,
    )

    # Warn if a different repo already uses this name
    if repo_name in cfg.repos and os.path.normcase(str(Path(cfg.repos[repo_name].path).resolve())) != cwd_norm:
        click.confirm(
            f"A repo named '{repo_name}' is already registered at "
            f"{cfg.repos[repo_name].path}. Replace it?",
            abort=True,
        )

    cfg.add_repo(
        name=repo_name,
        bot_token=bot_token,
        path=cwd,
        agent_backend=agent_backend,
    )

    click.echo(
        f"\nChatter registered '{repo_name}' at {cwd}.\n"
        f"  Agent: {agent_label(agent_backend)}\n"
        f"  Config: {GLOBAL_CONFIG_FILE}\n"
        f"\nRun `chatter` to start the bot."
    )


def _run_start() -> None:
    """Start the bot for the current repository."""
    cfg, repo_name, repo = _load_repo_config()
    _ensure_agent_auth(repo.agent_backend)
    BotConfig, run_bot = _import_bot_runtime()
    config = BotConfig(
        bot_token=repo.bot_token,
        allowed_user_id=cfg.allowed_user_id,
        repo_path=str(Path(repo.path).resolve()),
        repo_name=repo_name,
        agent_backend=repo.agent_backend,
    )
    run_bot(config)


@main.command(hidden=True)
def start() -> None:
    """Backward-compatible alias for starting the bot."""
    _run_start()


@main.command()
@click.argument("context_hint", default="unknown")
def notify(context_hint: str) -> None:
    """Send a Telegram startup notification (used by CLAUDE.md / AGENTS.md)."""
    cfg, _name, repo = _load_repo_config()
    send_startup_notification(
        bot_token=repo.bot_token,
        chat_id=cfg.allowed_user_id,
        context_hint=context_hint,
        agent_name=agent_label(repo.agent_backend),
    )
