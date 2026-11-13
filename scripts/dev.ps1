#!/usr/bin/env pwsh
<#
.SYNOPSIS
  One command from a fresh clone to a running console on Windows.

.DESCRIPTION
  Creates a virtual environment, installs the project, copies .env.example to
  .env if you have not, seeds the world and runs the whole loop, then serves
  the console at http://localhost:8787.

  Everything it does is a step you could run by hand -- it exists so the first
  five minutes of the project are not spent on setup.

.EXAMPLE
  .\scripts\dev.ps1
  .\scripts\dev.ps1 -SkipDemo     # serve whatever is already in the database
  .\scripts\dev.ps1 -SetupOnly    # install and check, then stop
  .\scripts\dev.ps1 -Port 8080
#>
param(
  [int]$Port = 8787,
  [switch]$SkipDemo,
  [switch]$SetupOnly,
  [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step([string]$text) { Write-Host "`n>> $text" -ForegroundColor Cyan }

# --- python -----------------------------------------------------------------
$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" }
      elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" }
      else { $null }
if (-not $py) {
  Write-Host "Python 3.11+ is not on PATH. https://python.org/downloads" -ForegroundColor Red
  exit 1
}

$version = & $py -c "import sys;print('.'.join(map(str,sys.version_info[:2])))"
Write-Host "python $version"
$parts = $version.Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) {
  Write-Host "DataSentry needs Python 3.11 or newer (found $version)." -ForegroundColor Red
  exit 1
}

# --- virtual environment ----------------------------------------------------
if (-not (Test-Path ".venv")) {
  Step "creating .venv"
  & $py -m venv .venv
}
$venvPy = Join-Path $root ".venv\Scripts\python.exe"

Step "installing dependencies"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -e ".[dev]" --quiet

# --- configuration ----------------------------------------------------------
if (-not (Test-Path ".env")) {
  Step "creating .env from .env.example"
  Copy-Item ".env.example" ".env"
  Write-Host "  Add a free GROQ_API_KEY or GEMINI_API_KEY to .env for model-backed"
  Write-Host "  reasoning. Without one the deterministic reasoner answers, which"
  Write-Host "  still runs the whole loop."
}

Step "checking the environment"
& $venvPy ds.py doctor --offline

if ($SetupOnly) {
  Write-Host "`nready  .\.venv\Scripts\python.exe ds.py demo, then ds.py serve" -ForegroundColor Green
  exit 0
}

# --- data -------------------------------------------------------------------
if (-not $SkipDemo) {
  Step "seeding the world and running the loop (a few minutes)"
  & $venvPy ds.py demo
}

# --- serve ------------------------------------------------------------------
Step "starting the console on http://localhost:$Port"
$serveArgs = @("ds.py", "serve", "--port", "$Port")
if (-not $NoBrowser) { $serveArgs += "--open" }
& $venvPy @serveArgs
