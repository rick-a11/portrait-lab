#!/usr/bin/env zsh
set -euo pipefail

# Start the Portrait Lab MCP server over stdio for Claude Desktop, Claude Code,
# or any other MCP client. stdout carries the protocol, so diagnostics go to stderr.

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_PATH="$PROJECT_ROOT/.venv-gfpgan/bin/python"
WEIGHT_PATH="$PROJECT_ROOT/models/GFPGANv1.4.pth"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime/gfpgan"

if [[ ! -x "$PYTHON_PATH" ]]; then
  print -u2 "Missing Python 3.10 runtime. Run scripts/bootstrap-gfpgan-macos.sh first."
  exit 1
fi

if [[ -z "${MODEL_MODE:-}" ]]; then
  if [[ -f "$WEIGHT_PATH" ]]; then
    export MODEL_MODE=gfpgan
  else
    print -u2 "GFPGAN weight not found; restore_photo will run in the labeled preview mode."
    export MODEL_MODE=preview
  fi
fi

mkdir -p "$RUNTIME_ROOT"
# GFPGAN resolves its helper weights relative to the working directory, like the API launcher.
cd "$RUNTIME_ROOT"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export GFPGAN_MODEL_PATH="${GFPGAN_MODEL_PATH:-$WEIGHT_PATH}"
export GFPGAN_DEVICE="${GFPGAN_DEVICE:-cpu}"
exec "$PYTHON_PATH" -m backend.mcp_server
