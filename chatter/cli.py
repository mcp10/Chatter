"""CLI entry points for Chatter."""

from __future__ import annotations

from pathlib import Path

import click

from .bot import BotConfig, run_bot
from .config import GLOBAL_CONFIG_FILE, REPO_CONFIG_NAME, GlobalConfig, RepoConfig
from .notify import send_startup_notification

ENV_FILE_NAME = ".env"
ENV_BLOCK_START = "# --- chatter managed ---"
ENV_BLOCK_END = "# --- /chatter managed ---"


def _upsert_local_env(bot_token: str, allowed_user_id: int, repo_name: str) -> tuple[Path, str]:
    env_path = Path.cwd() / ENV_FILE_NAME
    managed_block = (
        f"{ENV_BLOCK_START}\n"
        f"BOT_TOKEN={bot_token}\n"
        f"ALLOWED_USER_ID={allowed_user_id}\n"
        f"REPO_NAME={repo_name}\n"
        f"{ENV_BLOCK_END}\n"
    )

    if env_path.exists():
        content = env_path.read_text()
        if ENV_BLOCK_START in content and ENV_BLOCK_END in content:
            start = content.index(ENV_BLOCK_START)
            end = content.index(ENV_BLOCK_END) + len(ENV_BLOCK_END)
            updated = content[:start] + managed_block + content[end:]
            action = "Updated existing managed block in"
        else:
            updated = content.rstrip("\n") + "\n\n" + managed_block
            action = "Appended to existing"
    else:
        updated = managed_block
        action = "Created"

    env_path.write_text(updated)
    return env_path, action


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
        allowed_user_id = global_cfg.allowed_user_id
        click.echo(f"Using existing global user ID: {global_cfg.allowed_user_id}")
    else:
        user_id = click.prompt("Enter your Telegram user ID (allowed_user_id)", type=int)
        GlobalConfig.save(user_id)
        allowed_user_id = user_id
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
    required_ignores = [REPO_CONFIG_NAME, ENV_FILE_NAME]
    if gitignore.exists():
        content = gitignore.read_text()
        lines = [l.strip() for l in content.splitlines()]
        missing = [entry for entry in required_ignores if entry not in lines]
        if missing:
            additions = "\n".join(missing)
            gitignore.write_text(content.rstrip("\n") + f"\n{additions}\n")
            click.echo(f"Added {', '.join(missing)} to .gitignore")
        else:
            click.echo(f"{REPO_CONFIG_NAME} and {ENV_FILE_NAME} already in .gitignore")
    else:
        gitignore.write_text(f"{REPO_CONFIG_NAME}\n{ENV_FILE_NAME}\n")
        click.echo(f"Created .gitignore with {REPO_CONFIG_NAME} and {ENV_FILE_NAME}")

    env_path, env_action = _upsert_local_env(
        bot_token=bot_token,
        allowed_user_id=allowed_user_id,
        repo_name=repo_name,
    )
    click.echo(f"{env_action} {env_path}")

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
