#!/usr/bin/env bash
set -euo pipefail

PACKAGE="${BRAIN_PACKAGE:-git+https://github.com/Cognio-so/AI_computer_brain}"
REPO_URL="${BRAIN_REPO_URL:-https://github.com/Cognio-so/AI_computer_brain}"
INSTALL_DIR="${BRAIN_INSTALL_DIR:-$HOME/ai-computer-brain}"
MODE="${1:-tool}"

say() { printf '\033[1;34m[brain]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[brain]\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

ensure_uv() {
  if have uv; then return; fi
  say "installing uv, the Python manager (one time)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  have uv || die "uv was installed but is not on PATH yet. Open a new terminal and run this command again."
}

install_tool() {
  ensure_uv
  say "installing AI Computer Brain (Python 3.12 is downloaded if needed)"
  uv tool install --python 3.12 --force "$PACKAGE"
  uv tool update-shell >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
  say "installed. Its folder is ${BRAIN_HOME:-$HOME/.brain}."
  if [ -t 0 ]; then
    say "starting the console; next time just type: brain"
    exec brain
  fi
  say "open a new terminal and type: brain"
}

install_with_docker() {
  have git || die "git is required"
  if [ ! -d "$INSTALL_DIR/.git" ]; then
    say "cloning into $INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  cd "$INSTALL_DIR"
  if [ ! -f .env ]; then
    cp .env.example .env
    key="bm_$(head -c 36 /dev/urandom | base64 | tr -d '/+=\n' | head -c 48)"
    sed -i.bak "s|^ADMIN_MASTER_KEY=.*|ADMIN_MASTER_KEY=${key}|" .env && rm -f .env.bak
    say "generated ADMIN_MASTER_KEY and wrote .env"
  fi
  if [ -n "${BRAIN_DOMAIN:-}" ]; then
    sed -i.bak "s|^BRAIN_DOMAIN=.*|BRAIN_DOMAIN=${BRAIN_DOMAIN}|; s|^PUBLIC_URL=.*|PUBLIC_URL=https://${BRAIN_DOMAIN}|" .env && rm -f .env.bak
    say "starting with Caddy for https://${BRAIN_DOMAIN}"
    docker compose up -d --build
    say "admin panel: https://${BRAIN_DOMAIN}/admin"
  else
    say "starting local stack on http://localhost:8000"
    docker compose -f docker-compose.local.yml up -d --build
    say "admin panel: http://localhost:8000/admin"
  fi
  say "master key:"
  grep '^ADMIN_MASTER_KEY=' .env | cut -d= -f2-
}

install_from_source() {
  have git || die "git is required"
  if [ ! -d "$INSTALL_DIR/.git" ]; then
    say "cloning into $INSTALL_DIR"
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
  cd "$INSTALL_DIR"
  exec ./start.sh
}

case "$MODE" in
  tool|auto|pipx|python) install_tool ;;
  docker) install_with_docker ;;
  source|dev) install_from_source ;;
  *) die "usage: install.sh [tool|docker|source]" ;;
esac
