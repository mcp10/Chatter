# Chatter

Telegram bot that bridges messages to a local Codex or Claude agent.

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

`chatter init` now prompts for the repo's agent backend. Existing config entries that
do not have `agent_backend` continue to use Claude for backward compatibility.

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
    agent_backend: "codex"
```

No secrets are ever stored inside a repo directory.

## Agent Backends

- `codex` runs the local `codex` CLI through `codex exec --json` and preserves session continuity with `codex exec resume`.
- `claude` keeps the existing Claude SDK bridge, including Telegram approval prompts for write/shell tools.
- New repos default to the first available logged-in backend, preferring Codex when both are ready.

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

   This registers the directory in `~/.chatter/config.yaml` and stores the selected
   `agent_backend`. No files are created in the repo.

4. **Add the startup hook to your agent instructions file** (`CLAUDE.md`, `AGENTS.md`, etc.):

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

### `Codex login required.`

The selected repo backend is `codex`, but the local Codex CLI is missing or logged out.

```bash
codex login
```

If you prefer API-key auth instead of ChatGPT login:

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

### `ModuleNotFoundError: No module named 'claude_agent_sdk'`

This only affects repos configured with the `claude` backend. It means the `chatter`
launcher is running under a Python environment that does not have Chatter's runtime
dependencies installed.

```bash
which chatter
head -n 1 "$(which chatter)"   # shows the Python interpreter used by chatter
```

Then install/reinstall into that same interpreter:

```bash
<python-from-shebang> -m pip install --upgrade "claude-agent-sdk>=0.1.44"
<python-from-shebang> -m pip install --upgrade --force-reinstall "git+https://github.com/mcp10/Chatter.git"
```

### `Fatal error in message reader: Command failed with exit code 1`

If this happens after `AssistantMessage` or `ResultMessage`, you are likely running an
older Chatter build that sends single Telegram prompts through the SDK's streaming-input
path. That path is prone to transport shutdown failures in some Claude SDK versions.

Reinstall Chatter into the same Python environment used by the `chatter` command:

```bash
CHATTER_PY="$(head -n 1 "$(which chatter)" | sed 's|^#!||')"
"$CHATTER_PY" -m pip install --upgrade --force-reinstall "git+https://github.com/mcp10/Chatter.git"
```
