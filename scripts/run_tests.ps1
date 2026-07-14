# ─────────────────────────────────────────────────────────────────────────────
# FORGE Test Runner (PowerShell)
# Usage:  .\scripts\run_tests.ps1 [-Suite unit|integration|e2e|all]
#
# Requires Python 3.11+ and the project virtualenv to be active, or
# dependencies installed globally.  No pytest required — pure unittest.
# ─────────────────────────────────────────────────────────────────────────────
param(
    [ValidateSet("unit","integration","e2e","all")]
    [string]$Suite = "all"
)

$ErrorActionPreference = "Stop"

# ── Resolve repo root ─────────────────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
Set-Location $RepoRoot

# ── Activate virtualenv if present ───────────────────────────────────────────
$VenvActivate = Join-Path $RepoRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $VenvActivate) {
    & $VenvActivate
}

# ── Helpers ───────────────────────────────────────────────────────────────────
function Write-Ok   { param($msg) Write-Host "✓ $msg" -ForegroundColor Green }
function Write-Fail { param($msg) Write-Host "✗ $msg" -ForegroundColor Red }
function Write-Info { param($msg) Write-Host $msg -ForegroundColor Yellow }

function Invoke-Suite {
    param([string]$Name, [string]$Dir)
    Write-Info "`nRunning $Name tests ($Dir)…"
    $output = python -m unittest discover -s $Dir -p "test_*.py" -v 2>&1
    $output | Write-Host
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "$Name tests PASSED"
        return $true
    } else {
        Write-Fail "$Name tests FAILED"
        return $false
    }
}

Write-Host ""
Write-Host "════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  FORGE Test Suite" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

$Failed = $false

switch ($Suite) {
    "unit"        { if (-not (Invoke-Suite "Unit"        "tests\unit"))        { $Failed = $true } }
    "integration" { if (-not (Invoke-Suite "Integration" "tests\integration")) { $Failed = $true } }
    "e2e"         { if (-not (Invoke-Suite "End-to-end"  "tests\e2e"))         { $Failed = $true } }
    "all" {
        if (-not (Invoke-Suite "Unit"        "tests\unit"))        { $Failed = $true }
        if (-not (Invoke-Suite "Integration" "tests\integration")) { $Failed = $true }
        if (-not (Invoke-Suite "End-to-end"  "tests\e2e"))         { $Failed = $true }
    }
}

Write-Host ""
Write-Host "════════════════════════════════════════════════" -ForegroundColor Cyan
if (-not $Failed) {
    Write-Ok "All suites PASSED"
    exit 0
} else {
    Write-Fail "One or more suites FAILED"
    exit 1
}
