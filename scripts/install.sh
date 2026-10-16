#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${BRAIN_REPO_URL:-https://github.com/ai-computer-brain/ai-computer-brain}"
INSTALL_DIR="${BRAIN_INSTALL_DIR:-$HOME/ai-computer-brain}"
MODE="${1:-auto}"

say() { printf '\033[1;34m[brain]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[brain]\033[0m %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

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

install_with_pipx() {
  if have pipx; then
    pipx install --force ai-computer-brain
  elif have uv; then
    uv tool install --force ai-computer-brain
  elif have python3; then
    python3 -m pip install --user --upgrade ai-computer-brain
  else
    die "python3, pipx or uv is required"
  fi
  brain init
  say "run 'brain' to open the console; its folder is \${BRAIN_HOME:-\$HOME/.brain}"
}

case "$MODE" in
  docker) install_with_docker ;;
  pipx|python) install_with_pipx ;;
  auto)
    if have docker && docker compose version >/dev/null 2>&1; then
      install_with_docker
    else
      install_with_pipx
    fi
    ;;
  *) die "usage: install.sh [auto|docker|pipx]" ;;
esac
