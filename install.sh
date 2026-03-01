#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/mcp10/Chatter.git"

echo "Installing Chatter..."

# --- Check prerequisites ---
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required but not found." >&2
    exit 1
fi

if ! python3 -m pip --version &>/dev/null; then
    echo "Error: pip is required but not found." >&2
    exit 1
fi

# --- Install from GitHub ---
python3 -m pip install "git+${REPO}" --quiet

# --- Verify ---
if command -v chatter &>/dev/null; then
    echo "Chatter installed successfully!"
    echo ""
    echo "Next steps:"
    echo "  1. cd into your project directory"
    echo "  2. Run: chatter init"
    echo "  3. Run: chatter start"
else
    echo "Warning: 'chatter' command not found in PATH." >&2
    echo "You may need to add your Python scripts directory to PATH." >&2
    exit 1
fi
