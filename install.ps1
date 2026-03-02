# install.ps1 — Install Chatter on Windows
$ErrorActionPreference = "Stop"

$Repo = "https://github.com/mcp10/Chatter.git"

Write-Host "Installing Chatter..."

# Check prerequisites
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Error: python is required but not found."
    exit 1
}

if (-not (python -m pip --version 2>$null)) {
    Write-Error "Error: pip is required but not found."
    exit 1
}

# Install from GitHub
python -m pip install "git+$Repo" --quiet

# Verify
if (Get-Command chatter -ErrorAction SilentlyContinue) {
    Write-Host "Chatter installed successfully!"
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. cd into your project directory"
    Write-Host "  2. Run: chatter init"
    Write-Host "  3. Run: chatter start"
} else {
    Write-Error "Warning: 'chatter' command not found in PATH."
    Write-Error "You may need to add your Python scripts directory to PATH."
    exit 1
}
