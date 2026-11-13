#!/usr/bin/env pwsh
# Windows equivalent of the Makefile. `.\make.ps1 demo`, `.\make.ps1 test`.
param([Parameter(Position = 0)][string]$Target = "help",
      [Parameter(Position = 1)][string]$Type = "JOIN_SEMANTICS")

$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

switch ($Target) {
  "help"      { Write-Host "targets: setup doctor demo seed inject detect correlate rca execute verify serve eval test lint status reset docker" }
  "setup"     { & "$PSScriptRoot\scripts\dev.ps1" -SetupOnly }
  "doctor"    { & $py ds.py doctor }
  "demo"      { & $py ds.py demo }
  "seed"      { & $py ds.py seed --fresh }
  "inject"    { & $py ds.py inject $Type --advance 18 }
  "detect"    { & $py ds.py detect --cycles 18 }
  "correlate" { & $py ds.py correlate }
  "rca"       { & $py ds.py rca --verbose }
  "execute"   { & $py ds.py execute }
  "verify"    { & $py ds.py verify }
  "serve"     { & $py ds.py serve --open }
  "eval"      { & $py ds.py eval }
  "test"      { & $py -m pytest }
  "lint"      { & $py -m ruff check . }
  "status"    { & $py ds.py status }
  "reset"     { & $py ds.py reset }
  "docker"    { docker build -t datasentry:local . }
  "docker-run" { docker compose up --build }
  default     { Write-Host "unknown target: $Target"; exit 1 }
}
