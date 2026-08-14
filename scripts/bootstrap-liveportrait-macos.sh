#!/usr/bin/env zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime/liveportrait"
VENV_PATH="$PROJECT_ROOT/.venv-liveportrait"
CERT_PATH="${SSL_CERT_FILE:-/etc/ssl/cert.pem}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  print -u2 "FFmpeg is required. Install it first with: brew install ffmpeg"
  exit 1
fi
if [[ ! -f "$RUNTIME_ROOT/inference.py" ]]; then
  mkdir -p "$PROJECT_ROOT/.runtime"
  git clone --depth 1 https://github.com/KwaiVGI/LivePortrait.git "$RUNTIME_ROOT"
fi
if [[ ! -x "$VENV_PATH/bin/python" ]]; then
  uv venv --python 3.10 "$VENV_PATH"
fi

SSL_CERT_FILE="$CERT_PATH" uv pip install --index-strategy unsafe-best-match \
  --python "$VENV_PATH/bin/python" -r "$RUNTIME_ROOT/requirements_macOS.txt"
# LivePortrait's vendored InsightFace helper imports requests, but the upstream
# macOS requirements file does not currently declare it.
SSL_CERT_FILE="$CERT_PATH" uv pip install --python "$VENV_PATH/bin/python" requests

LIVEPORTRAIT_WEIGHTS="$RUNTIME_ROOT/pretrained_weights" "$VENV_PATH/bin/python" - <<'PY'
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="KlingTeam/LivePortrait",
    local_dir=os.environ["LIVEPORTRAIT_WEIGHTS"],
    allow_patterns=[
        "liveportrait/base_models/*",
        "liveportrait/retargeting_models/stitching_retargeting_module.pth",
        "liveportrait/landmark.onnx",
        "insightface/models/buffalo_l/2d106det.onnx",
        "insightface/models/buffalo_l/det_10g.onnx",
    ],
)
PY

print "LivePortrait source, Python environment, and local weights are ready."
