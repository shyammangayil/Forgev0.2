# FORGE Local Setup Script
# Requires: Python 3.12

# Determine repository root (parent of scripts/)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir

Set-Location $RepoRoot

$ErrorActionPreference = "Stop"

function Info($msg) {
    Write-Host "  ✓ $msg" -ForegroundColor Green
}

function Warn($msg) {
    Write-Host "  ⚠ $msg" -ForegroundColor Yellow
}

function Fail($msg) {
    Write-Host "  ✗ $msg" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  ███████╗ ██████╗ ██████╗  ██████╗ ███████╗"
Write-Host "  ██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝"
Write-Host "  █████╗  ██║   ██║██████╔╝██║  ███╗█████╗"
Write-Host "  ██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝"
Write-Host "  ██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗"
Write-Host "  ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝"
Write-Host ""
Write-Host "  Setting up FORGE..."
Write-Host ""

# Verify Python 3.12 exists
try {
    $pyVersion = py -3.12 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
}
catch {
    Fail "Python 3.12 not found. Install Python 3.12 and ensure py launcher is available."
}

Info "Python $pyVersion found"

# Create venv
if (-not (Test-Path ".venv")) {
    py -3.12 -m venv .venv
    Info "Virtual environment created (.venv)"
}
else {
    Info "Virtual environment already exists (.venv)"
}

# Activate venv
& ".\.venv\Scripts\Activate.ps1"

# Upgrade pip
python -m pip install --upgrade pip --quiet
Info "pip upgraded"

# Install dependencies
pip install -r requirements.txt --quiet
Info "Dependencies installed"

# Install FORGE
python -m pip install --upgrade pip setuptools wheel
pip install -e .

if ($LASTEXITCODE -ne 0) {
    Fail "FORGE installation failed"
}

Info "FORGE installed (forge command now available)"

# Create .env
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Warn ".env created from .env.example — add your API keys before running"
}
else {
    Info ".env already exists"
}

# Create directories
New-Item -ItemType Directory -Force -Path TASKS, workspace, logs | Out-Null
Info "Directories created (TASKS/, workspace/, logs/)"

Write-Host ""
Write-Host "  Running tests to verify installation..."

#python -m unittest discover tests/ -v 2>&1 |
#    Select-Object -Last 5

#Write-Host ""

#Info "FORGE setup complete!"

python -m unittest discover tests -v

if ($LASTEXITCODE -ne 0) {
    Fail "Test suite failed"
}

Info "All tests passed"

Write-Host ""
Write-Host "  Next steps:"
Write-Host "    1. Add at least one API key to .env"
Write-Host "    2. Activate virtual environment"
Write-Host "       .\.venv\Scripts\Activate.ps1"
Write-Host ""
Write-Host "    3. Check providers"
Write-Host "       forge health"
Write-Host ""
Write-Host "    4. Run your first task"
Write-Host '       forge run "Build a hello world REST endpoint"'
Write-Host ""