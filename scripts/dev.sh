#!/usr/bin/env bash
# One command from a fresh clone to a running console on macOS and Linux.
#
#   ./scripts/dev.sh                  set up, run the loop, serve
#   ./scripts/dev.sh --skip-demo      serve whatever is already in the database
#   ./scripts/dev.sh --setup-only     install and check, then stop
#   ./scripts/dev.sh --port 8080
#
# Everything here is a step you could run by hand. It exists so the first five
# minutes of the project are not spent on setup.

set -euo pipefail

cd "$(dirname "$0")/.."

PORT=8787
SKIP_DEMO=0
SETUP_ONLY=0
OPEN_FLAG="--open"

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --skip-demo) SKIP_DEMO=1; shift ;;
    --setup-only) SETUP_ONLY=1; shift ;;
    --no-browser) OPEN_FLAG=""; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

step() { printf '\n\033[36m>> %s\033[0m\n' "$1"; }

# --- python -----------------------------------------------------------------
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.11+ is not on PATH." >&2
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "DataSentry needs Python 3.11 or newer (found $("$PY" -V))." >&2
  exit 1
fi
echo "$("$PY" -V)"

# --- virtual environment ----------------------------------------------------
if [ ! -d .venv ]; then
  step "creating .venv"
  "$PY" -m venv .venv
fi
VENV_PY=".venv/bin/python"

step "installing dependencies"
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -e ".[dev]" --quiet

# --- configuration ----------------------------------------------------------
if [ ! -f .env ]; then
  step "creating .env from .env.example"
  cp .env.example .env
  echo "  Add a free GROQ_API_KEY or GEMINI_API_KEY to .env for model-backed"
  echo "  reasoning. Without one the deterministic reasoner answers, which"
  echo "  still runs the whole loop."
fi

step "checking the environment"
"$VENV_PY" ds.py doctor --offline

if [ "$SETUP_ONLY" -eq 1 ]; then
  printf '
[32mready[0m  .venv/bin/python ds.py demo, then ds.py serve
'
  exit 0
fi

# --- data -------------------------------------------------------------------
if [ "$SKIP_DEMO" -eq 0 ]; then
  step "seeding the world and running the loop (a few minutes)"
  "$VENV_PY" ds.py demo
fi

# --- serve ------------------------------------------------------------------
step "starting the console on http://localhost:${PORT}"
# shellcheck disable=SC2086
exec "$VENV_PY" ds.py serve --port "$PORT" $OPEN_FLAG
