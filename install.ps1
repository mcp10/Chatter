# install.ps1 — Install Chatter on Windows
$ErrorActionPreference = "Stop"

$Repo = "https://github.com/mcp10/Chatter.git"

Write-Host "Installing Chatter..."

# Check prerequisites
# Prefer the Windows launcher when available.
$PythonExe = $null
$PythonBaseArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"
    $PythonBaseArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = "python"
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $PythonExe = "python3"
} else {
    Write-Error "Error: Python is required but was not found (tried py, python, python3)."
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "Error: git is required but not found."
    exit 1
}

$null = & $PythonExe @PythonBaseArgs -m pip --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Error: pip is required but not found."
    exit 1
}

$PyVersion = & $PythonExe @PythonBaseArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
$null = & $PythonExe @PythonBaseArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Error: Chatter requires Python 3.10+ (found $PyVersion)."
    exit 1
}

# Install from GitHub
& $PythonExe @PythonBaseArgs -m pip install --upgrade --force-reinstall "git+$Repo" --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Error "Error: installation failed."
    exit 1
}

# Verify
if (Get-Command chatter -ErrorAction SilentlyContinue) {
    Write-Host "Chatter installed successfully!"
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. cd into your project directory"
    Write-Host "  2. Run: chatter init"
    Write-Host "  3. Run: chatter"
} else {
    Write-Error "Warning: 'chatter' command not found in PATH."
    Write-Error "You may need to add your Python scripts directory to PATH."
    exit 1
}
