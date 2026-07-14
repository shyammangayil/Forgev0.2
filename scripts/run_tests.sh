#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# FORGE Test Runner
# Usage:  ./scripts/run_tests.sh [unit|integration|e2e|all]
#
# Requires Python 3.11+ and the project virtualenv to be active, or
# dependencies installed globally.  No pytest required — pure unittest.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Resolve repo root ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# ── Activate virtualenv if present ────────────────────────────────────────────
if [[ -f ".venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
fi

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN="\033[0;32m"; RED="\033[0;31m"; YELLOW="\033[0;33m"; RESET="\033[0m"; BOLD="\033[1m"
ok()   { echo -e "${GREEN}${BOLD}✓ $*${RESET}"; }
fail() { echo -e "${RED}${BOLD}✗ $*${RESET}"; }
info() { echo -e "${YELLOW}$*${RESET}"; }

# ── Parse arguments ───────────────────────────────────────────────────────────
SUITE="${1:-all}"

run_suite() {
    local name="$1"
    local dir="$2"
    info "\nRunning ${name} tests (${dir})…"
    if python -m unittest discover -s "${dir}" -p "test_*.py" -v 2>&1; then
        ok "${name} tests PASSED"
        return 0
    else
        fail "${name} tests FAILED"
        return 1
    fi
}

echo ""
echo -e "${BOLD}════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}  FORGE Test Suite${RESET}"
echo -e "${BOLD}════════════════════════════════════════════════${RESET}"
echo ""

FAILED=0

case "${SUITE}" in
    unit)
        run_suite "Unit" "tests/unit" || FAILED=1
        ;;
    integration)
        run_suite "Integration" "tests/integration" || FAILED=1
        ;;
    e2e)
        run_suite "End-to-end" "tests/e2e" || FAILED=1
        ;;
    all)
        run_suite "Unit"        "tests/unit"        || FAILED=1
        run_suite "Integration" "tests/integration" || FAILED=1
        run_suite "End-to-end"  "tests/e2e"         || FAILED=1
        ;;
    *)
        echo "Usage: $0 [unit|integration|e2e|all]" >&2
        exit 1
        ;;
esac

echo ""
echo -e "${BOLD}════════════════════════════════════════════════${RESET}"
if [[ $FAILED -eq 0 ]]; then
    ok "All suites PASSED"
else
    fail "One or more suites FAILED"
fi
echo ""
exit $FAILED
