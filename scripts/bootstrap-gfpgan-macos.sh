#!/usr/bin/env zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PATH="$PROJECT_ROOT/.venv-gfpgan"
CERT_PATH="${SSL_CERT_FILE:-/etc/ssl/cert.pem}"

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

print "GFPGAN Python 3.10 environment is ready at $VENV_PATH"
