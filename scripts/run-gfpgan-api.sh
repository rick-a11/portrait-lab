#!/usr/bin/env zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_PATH="$PROJECT_ROOT/.venv-gfpgan/bin/python"
WEIGHT_PATH="$PROJECT_ROOT/models/GFPGANv1.4.pth"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime/gfpgan"

if [[ ! -x "$PYTHON_PATH" ]]; then
  print -u2 "Missing Python 3.10 runtime. Run scripts/bootstrap-gfpgan-macos.sh first."
  exit 1
fi
if [[ ! -f "$WEIGHT_PATH" ]]; then
  print -u2 "Missing GFPGAN weight: $WEIGHT_PATH"
  exit 1
fi

mkdir -p "$RUNTIME_ROOT"
cd "$RUNTIME_ROOT"
export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export MODEL_MODE=gfpgan
export GFPGAN_MODEL_PATH="$WEIGHT_PATH"
export GFPGAN_DEVICE="${GFPGAN_DEVICE:-cpu}"
exec "$PYTHON_PATH" -m flask --app backend.app run --host 127.0.0.1 --port "${PORTRAIT_LAB_PORT:-5000}"
