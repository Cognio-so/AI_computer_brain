#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\033[1;36m[brain]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[brain]\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3 python; do
    if have "$candidate" && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_uv() {
  have uv && return 0
  say "installing uv, the Python manager (one time)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || return 1
  export PATH="$HOME/.local/bin:$PATH"
  have uv
}

PYTHON=".venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  if have uv; then
    say "creating the virtual environment"
    uv venv --python 3.12 .venv >/dev/null
  elif found=$(find_python); then
    say "creating the virtual environment with $found"
    "$found" -m venv .venv
  elif ensure_uv; then
    say "creating the virtual environment"
    uv venv --python 3.12 .venv >/dev/null
  else
    fail "Python 3.11+ was not found and uv could not be installed. Install Python 3.11 or newer and run this script again."
  fi
fi

if ! "$PYTHON" -c 'import second_brain' 2>/dev/null; then
  say "installing AI Computer Brain and its dependencies (first run only)"
  if have uv; then
    uv pip install --python "$PYTHON" -q -e .
  else
    "$PYTHON" -m pip install -q --upgrade pip
    "$PYTHON" -m pip install -q -e .
  fi
fi

say "starting AI Computer Brain"
exec "$PYTHON" -m second_brain.cli shell --open "$@"
