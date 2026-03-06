# Chatter

Telegram bot that bridges messages to the local Claude CLI agent.

Requires Python 3.10+.

## Quick Install

**macOS / Linux:**

```bash
curl -sSL https://raw.githubusercontent.com/mcp10/Chatter/main/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/mcp10/Chatter/main/install.ps1 | iex
```

## Quick Start

```bash
chatter init
chatter
```

## Commands

```bash
# First-time setup (run once globally, then once per repo)
chatter init

# Start the bot (from within the repo directory)
chatter

# Send a one-off notification
chatter notify "message"
```

## Reinstall / Upgrade

Reinstall Chatter into the same Python environment used by the `chatter` command:

```bash
CHATTER_PY="$(head -n 1 "$(which chatter)" | sed 's|^#!||')"
"$CHATTER_PY" -m pip install --upgrade --force-reinstall "git+https://github.com/mcp10/Chatter.git"
```

## Config

All configuration is stored in a single file outside any repository:

```
~/.chatter/config.yaml
```

```yaml
allowed_user_id: 877944241
repos:
  MyProject:
    bot_token: "123:ABC..."
    path: "/Users/you/projects/MyProject"
```

No secrets are ever stored inside a repo directory.

## Adding Chatter to a new project

1. **Create a Telegram bot** via [@BotFather](https://t.me/BotFather) and copy the token.

2. **Install Chatter** (once):

   ```bash
   # macOS/Linux
   curl -sSL https://raw.githubusercontent.com/mcp10/Chatter/main/install.sh | bash

   # Windows (PowerShell)
   irm https://raw.githubusercontent.com/mcp10/Chatter/main/install.ps1 | iex
   ```

3. **Run `chatter init`** inside the new project directory:

   ```bash
   cd <your-project>
   chatter init
   ```

   This registers the directory in `~/.chatter/config.yaml`. No files are created in the repo.

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
   chatter
   ```

## Troubleshooting

### `ModuleNotFoundError: No module named 'claude_agent_sdk'`

This means the `chatter` launcher is running under a Python environment that does not
have Chatter's runtime dependencies installed.

```bash
which chatter
head -n 1 "$(which chatter)"   # shows the Python interpreter used by chatter
```

Then install/reinstall into that same interpreter:

```bash
<python-from-shebang> -m pip install --upgrade "claude-agent-sdk>=0.1.44"
<python-from-shebang> -m pip install --upgrade --force-reinstall "git+https://github.com/mcp10/Chatter.git"
```
