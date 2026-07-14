#!/usr/bin/env bash
# FORGE Local Setup Script
# Requires: Python 3.11+
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}  ✓ $1${NC}"; }
warn()  { echo -e "${YELLOW}  ⚠ $1${NC}"; }
error() { echo -e "${RED}  ✗ $1${NC}"; exit 1; }

echo ""
echo "  ███████╗ ██████╗ ██████╗  ██████╗ ███████╗"
echo "  ██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝"
echo "  █████╗  ██║   ██║██████╔╝██║  ███╗█████╗  "
echo "  ██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝  "
echo "  ██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗"
echo "  ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝"
echo ""
echo "  Setting up FORGE..."
echo ""

# Check Python version
PYTHON=$(command -v python3 || command -v python || error "Python 3.11+ required")
PYVER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYMAJ=$($PYTHON -c "import sys; print(sys.version_info.major)")
PYMIN=$($PYTHON -c "import sys; print(sys.version_info.minor)")

if [ "$PYMAJ" -lt 3 ] || ([ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]); then
    error "Python 3.11+ required (found $PYVER)"
fi
info "Python $PYVER found"

# Create virtual environment
if [ ! -d ".venv" ]; then
    $PYTHON -m venv .venv
    info "Virtual environment created (.venv)"
else
    info "Virtual environment already exists (.venv)"
fi

# Activate venv
source .venv/bin/activate

# Upgrade pip
pip install --upgrade pip --quiet
info "pip upgraded"

# Install dependencies
pip install -r requirements.txt --quiet
info "Dependencies installed"

# Install FORGE in editable mode
pip install -e . --quiet
info "FORGE installed (forge command now available)"

# Create .env if not present
if [ ! -f ".env" ]; then
    cp .env.example .env
    warn ".env created from .env.example — add your API keys before running"
else
    info ".env already exists"
fi

# Create runtime directories
mkdir -p TASKS workspace logs
info "Directories created (TASKS/, workspace/, logs/)"

# Run tests to verify installation
echo ""
echo "  Running tests to verify installation..."
python -m unittest discover tests/ -v 2>&1 | tail -5
echo ""

info "FORGE setup complete!"
echo ""
echo "  Next steps:"
echo "    1. Add at least one API key to .env"
echo "       nano .env"
echo ""
echo "    2. Activate virtual environment"
echo "       source .venv/bin/activate"
echo ""
echo "    3. Check providers"
echo "       forge health"
echo ""
echo "    4. Run your first task"
echo "       forge run \"Build a hello world REST endpoint\""
echo ""
