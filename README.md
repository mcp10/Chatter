# Chatter

Telegram bot that bridges messages to the local Claude CLI agent.

## Setup

```bash
python3 -m pip install -e .
```

## Commands

```bash
# First-time setup in a repo (run once globally, then once per repo)
chatter init

# Start the bot (from within the repo directory)
chatter start

# Send a one-off notification
chatter notify "message"
```

## Config files

| File | Location | Contents |
| ---- | -------- | -------- |
| Global | `~/.chatter/config.yaml` | `allowed_user_id` |
| Per-repo | `.chatter.yaml` (gitignored) | `bot_token`, `repo_name` |
| Local env (optional runtime mirror) | `.env` (gitignored) | `BOT_TOKEN`, `ALLOWED_USER_ID`, `REPO_NAME` |

## Adding Chatter to a new project

1. **Create a Telegram bot** via [@BotFather](https://t.me/BotFather) and copy the token.

2. **Install Chatter** (once):

   ```bash
   python3 -m pip install -e <chatter-repo>
   ```

3. **Run `chatter init`** inside the new project directory:

   ```bash
   cd <your-project>
   chatter init
   ```

   This will ask for your Telegram user ID (first time only) and the bot token, then create `.chatter.yaml`, update `.env`, and ensure both are in `.gitignore`.

4. **Add the startup hook to `CLAUDE.md`** in the new project:

   ```markdown
   ## Session startup notification
   At the **very start of every conversation**, before doing anything else, run:
   \`\`\`
   chatter notify "<context>"
   \`\`\`
   Where `<context>` is `"with memory"` if memory exists, or `"fresh (no memory)"` if not.
   ```

5. **Start the bot**:

   ```bash
   chatter start
   ```

## Notes

- Keep `.chatter.yaml` and any token files out of version control.
