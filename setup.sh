#!/bin/bash
# Installs what Epilog needs (via uv) and starts the guided setup.
# Safe to run again: setup remembers finished steps.
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv, a small tool that sets up Python for Epilog…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv sync --quiet
uv run epilog setup
