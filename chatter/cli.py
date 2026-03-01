"""CLI entry points for Chatter."""

from __future__ import annotations

from pathlib import Path

import click

from .bot import BotConfig, run_bot
from .config import GLOBAL_CONFIG_FILE, REPO_CONFIG_NAME, GlobalConfig, RepoConfig
from .notify import send_startup_notification


def _load_configs() -> tuple[RepoConfig, GlobalConfig]:
    try:
        repo_cfg = RepoConfig.load()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    try:
        global_cfg = GlobalConfig.load()
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    return repo_cfg, global_cfg


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Chatter — Telegram bot bridging messages to a local Claude CLI agent."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(start)


@main.command()
def init() -> None:
    """Set up Chatter in the current repository."""
    # --- Global config (allowed_user_id) ---
    if GLOBAL_CONFIG_FILE.exists():
        global_cfg = GlobalConfig.load()
        click.echo(f"Using existing global user ID: {global_cfg.allowed_user_id}")
    else:
        user_id = click.prompt("Enter your Telegram user ID (allowed_user_id)", type=int)
        GlobalConfig.save(user_id)
        click.echo(f"Saved to {GLOBAL_CONFIG_FILE}")

    # --- Repo config (.chatter.yaml) ---
    repo_config_path = Path.cwd() / REPO_CONFIG_NAME
    if repo_config_path.exists():
        click.confirm(
            f"{REPO_CONFIG_NAME} already exists. Overwrite?", abort=True
        )

    bot_token = click.prompt("Enter the Telegram bot token for this repo (from BotFather)")
    default_name = Path.cwd().name
    repo_name = click.prompt("Repo name", default=default_name)

    RepoConfig.save(repo_config_path, bot_token=bot_token, repo_name=repo_name)
    click.echo(f"Created {repo_config_path}")

    # --- Gitignore ---
    gitignore = Path.cwd() / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if REPO_CONFIG_NAME not in [l.strip() for l in content.splitlines()]:
            gitignore.write_text(content.rstrip("\n") + f"\n{REPO_CONFIG_NAME}\n")
            click.echo(f"Added {REPO_CONFIG_NAME} to .gitignore")
        else:
            click.echo(f"{REPO_CONFIG_NAME} already in .gitignore")
    else:
        gitignore.write_text(f"{REPO_CONFIG_NAME}\n")
        click.echo(f"Created .gitignore with {REPO_CONFIG_NAME}")

    click.echo(
        f"\nChatter initialised for {repo_name}.\n"
        f"  Bot token : stored in {REPO_CONFIG_NAME} (gitignored)\n"
        f"  User ID   : stored in {GLOBAL_CONFIG_FILE}\n"
        f"\nRun `chatter` to start the bot."
    )


@main.command()
def start() -> None:
    """Start the bot for the current repository."""
    repo_cfg, global_cfg = _load_configs()
    config = BotConfig(
        bot_token=repo_cfg.bot_token,
        allowed_user_id=global_cfg.allowed_user_id,
        repo_path=str(Path.cwd()),
        repo_name=repo_cfg.repo_name,
    )
    run_bot(config)


@main.command()
@click.argument("context_hint", default="unknown")
def notify(context_hint: str) -> None:
    """Send a Telegram startup notification (used by CLAUDE.md)."""
    repo_cfg, global_cfg = _load_configs()
    send_startup_notification(
        bot_token=repo_cfg.bot_token,
        chat_id=global_cfg.allowed_user_id,
        context_hint=context_hint,
    )
