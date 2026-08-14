#!/usr/bin/env zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$PROJECT_ROOT/scripts/portrait-lab-service.zsh" start
