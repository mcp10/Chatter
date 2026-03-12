"""Shared agent-backend helpers."""

from __future__ import annotations

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"
DEFAULT_AGENT_BACKEND = AGENT_CLAUDE
SUPPORTED_AGENT_BACKENDS = (AGENT_CODEX, AGENT_CLAUDE)

_AGENT_LABELS = {
    AGENT_CLAUDE: "Claude Code",
    AGENT_CODEX: "Codex",
}

_AGENT_ALIASES = {
    "claude": AGENT_CLAUDE,
    "claude-code": AGENT_CLAUDE,
    "codex": AGENT_CODEX,
    "codex-cli": AGENT_CODEX,
}


def normalize_agent_backend(value: str | None, *, default: str = DEFAULT_AGENT_BACKEND) -> str:
    """Normalize persisted/user-provided backend names."""
    if value is None:
        return default

    normalized = _AGENT_ALIASES.get(value.strip().lower())
    if normalized is None:
        raise ValueError(
            f"Unsupported agent backend '{value}'. Expected one of: "
            f"{', '.join(SUPPORTED_AGENT_BACKENDS)}."
        )
    return normalized


def agent_label(agent_backend: str) -> str:
    """Return a human-readable label for a backend."""
    normalized = normalize_agent_backend(agent_backend)
    return _AGENT_LABELS[normalized]
