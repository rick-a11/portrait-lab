#!/usr/bin/env zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PATH="$PROJECT_ROOT/.venv-gfpgan"
WEIGHT_PATH="$PROJECT_ROOT/models/GFPGANv1.4.pth"
WEIGHT_URL="${GFPGAN_WEIGHT_URL:-https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth}"
RUNTIME_ROOT="$PROJECT_ROOT/.runtime/gfpgan"
HELPER_WEIGHTS_DIRECTORY="$RUNTIME_ROOT/gfpgan/weights"
DETECTION_WEIGHT_PATH="$HELPER_WEIGHTS_DIRECTORY/detection_Resnet50_Final.pth"
DETECTION_WEIGHT_URL="https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth"
PARSING_WEIGHT_PATH="$HELPER_WEIGHTS_DIRECTORY/parsing_parsenet.pth"
PARSING_WEIGHT_URL="https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth"
CERT_PATH="${SSL_CERT_FILE:-/etc/ssl/cert.pem}"

download_weight_if_missing() {
  local destination="$1"
  local source_url="$2"
  local temporary_download

  [[ -f "$destination" ]] && return 0
  mkdir -p "${destination:h}"
  temporary_download="$(mktemp "${destination:h}/.${destination:t}.download.XXXXXX")"
  print "Downloading ${destination:t} from its official upstream release..."
  "$VENV_PATH/bin/python" - "$source_url" "$temporary_download" <<'PY'
from pathlib import Path
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

url, destination = sys.argv[1:]
session = requests.Session()
session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
        )
    ),
)
with session.get(url, stream=True, timeout=(30, 120)) as response:
    response.raise_for_status()
    with Path(destination).open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
PY
  if [[ ! -s "$temporary_download" ]]; then
    print -u2 "The download for ${destination:t} was empty; no model was installed."
    exit 1
  fi
  mv "$temporary_download" "$destination"
}

if ! command -v uv >/dev/null 2>&1; then
  print -u2 "uv is required. Install it with: brew install uv"
  exit 1
fi
if [[ ! -x "$VENV_PATH/bin/python" ]]; then
  uv venv --python 3.10 "$VENV_PATH"
fi

"$VENV_PATH/bin/python" -m ensurepip --upgrade
SSL_CERT_FILE="$CERT_PATH" "$VENV_PATH/bin/pip3" install --disable-pip-version-check \
  'numpy==1.26.4' 'torch==2.2.2' 'torchvision==0.17.2' 'cython<3' wheel
SSL_CERT_FILE="$CERT_PATH" "$VENV_PATH/bin/pip3" install --disable-pip-version-check --no-deps --no-build-isolation \
  'basicsr==1.4.2'
SSL_CERT_FILE="$CERT_PATH" "$VENV_PATH/bin/pip3" install --disable-pip-version-check --no-deps \
  'gfpgan==1.3.8' 'facexlib==0.3.0' 'opencv-python==4.9.0.80'
SSL_CERT_FILE="$CERT_PATH" "$VENV_PATH/bin/pip3" install --disable-pip-version-check \
  'Flask==3.1.3' 'Flask-Cors==5.0.1' 'Pillow==12.3.0' 'scipy==1.11.4' \
  addict future lmdb pyyaml tb-nightly tqdm yapf requests scikit-image filterpy numba

download_weight_if_missing "$WEIGHT_PATH" "$WEIGHT_URL"
download_weight_if_missing "$DETECTION_WEIGHT_PATH" "$DETECTION_WEIGHT_URL"
download_weight_if_missing "$PARSING_WEIGHT_PATH" "$PARSING_WEIGHT_URL"

print "GFPGAN Python 3.10 environment and official model weights are ready."
