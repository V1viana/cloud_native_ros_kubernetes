#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
EXPERIMENT_MODE=e4 exec "$ROOT_DIR/scripts/run_p2.sh" "$@"
