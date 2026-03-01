"""CLI entry points for Chatter."""

from __future__ import annotations

from pathlib import Path

import click

from .bot import BotConfig, run_bot
from .config import GLOBAL_CONFIG_FILE, ChatterConfig
from .notify import send_startup_notification


def _load_config() -> ChatterConfig:
    """Load the central config, raising ClickException on failure."""
    try:
        return ChatterConfig.load()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))


def _load_repo_config() -> tuple[ChatterConfig, str, "RepoEntry"]:  # noqa: F821
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
    """Chatter — Telegram bot bridging messages to a local Claude CLI agent."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(start)


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
    existing_name = None
    for name, entry in cfg.repos.items():
        if str(Path(entry.path).resolve()) == cwd:
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

    # Warn if a different repo already uses this name
    if repo_name in cfg.repos and str(Path(cfg.repos[repo_name].path).resolve()) != cwd:
        click.confirm(
            f"A repo named '{repo_name}' is already registered at "
            f"{cfg.repos[repo_name].path}. Replace it?",
            abort=True,
        )

    cfg.add_repo(name=repo_name, bot_token=bot_token, path=cwd)

    click.echo(
        f"\nChatter registered '{repo_name}' at {cwd}.\n"
        f"  Config: {GLOBAL_CONFIG_FILE}\n"
        f"\nRun `chatter` to start the bot."
    )


@main.command()
def start() -> None:
    """Start the bot for the current repository."""
    cfg, repo_name, repo = _load_repo_config()
    config = BotConfig(
        bot_token=repo.bot_token,
        allowed_user_id=cfg.allowed_user_id,
        repo_path=str(Path.cwd()),
        repo_name=repo_name,
    )
    run_bot(config)


@main.command()
@click.argument("context_hint", default="unknown")
def notify(context_hint: str) -> None:
    """Send a Telegram startup notification (used by CLAUDE.md)."""
    cfg, _name, repo = _load_repo_config()
    send_startup_notification(
        bot_token=repo.bot_token,
        chat_id=cfg.allowed_user_id,
        context_hint=context_hint,
    )
