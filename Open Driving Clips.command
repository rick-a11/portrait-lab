#!/usr/bin/env zsh
set -euo pipefail

DEFAULT_PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${PORTRAIT_LAB_PROJECT_ROOT:-$DEFAULT_PROJECT_ROOT}"
DRIVING_DIRECTORY="$PROJECT_ROOT/.runtime/liveportrait/assets/examples/driving"
OPEN_COMMAND="${PORTRAIT_LAB_OPEN_COMMAND:-open}"

if [[ ! -d "$DRIVING_DIRECTORY" ]]; then
  print -u2 "LivePortrait driving clips are not available yet. Run scripts/bootstrap-liveportrait-macos.sh first."
  exit 1
fi

"$OPEN_COMMAND" "$DRIVING_DIRECTORY"
print "Opened the local LivePortrait driving-clips folder."
