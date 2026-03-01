# Chatter — Claude Code Instructions

## Session startup notification

At the **very start of every conversation**, before doing anything else, run:

```
chatter notify "<context>"
```

Where `<context>` describes whether you have memory or not:
- If `/Users/maximilienpicquet/.claude/projects/-Users-maximilienpicquet-VSC-projects-Chatter/memory/MEMORY.md` exists and was loaded → use `"with memory"`
- If it does not exist or is empty → use `"fresh (no memory)"`

This sends a small Telegram message to the user so they know the agent state.
