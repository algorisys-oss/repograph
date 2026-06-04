#!/usr/bin/env bash
#
# refresh-map.sh — (re)build a repograph index + cache for a project, incrementally.
#
# Re-run this any time; thanks to --cache only changed files are re-analyzed.
# Writes <repo>/.repograph/map.index and <repo>/.repograph/map.graph.json.
#
# Usage:
#   tools/refresh-map.sh [REPO_DIR] [extra repograph args...]
#
#   tools/refresh-map.sh                       # map the current directory
#   tools/refresh-map.sh /path/to/project      # map another project
#   tools/refresh-map.sh . --include 'src/*'   # pass through any repograph flag
#   tools/refresh-map.sh . --rebuild           # force a full re-analysis
#
# Finding repograph.py (in priority order):
#   1. $REPOGRAPH_PY  (set this if you copied this script out of the repo)
#   2. ../repograph.py relative to this script (the normal in-repo case)
#   3. `repograph.py` on $PATH
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -n "${REPOGRAPH_PY:-}" ]; then
  TOOL="$REPOGRAPH_PY"
elif [ -f "$SCRIPT_DIR/../repograph.py" ]; then
  TOOL="$SCRIPT_DIR/../repograph.py"
elif command -v repograph.py >/dev/null 2>&1; then
  TOOL="repograph.py"
else
  echo "refresh-map: cannot find repograph.py — set REPOGRAPH_PY=/path/to/repograph.py" >&2
  exit 1
fi

PY="${PYTHON:-python3}"

# Optional first arg = repo dir (must be an existing dir, not a flag).
REPO="$(pwd)"
if [ "${1:-}" ] && [ "${1#-}" = "${1:-}" ] && [ -d "${1:-}" ]; then
  REPO="$1"; shift
fi

OUT="$REPO/.repograph"
mkdir -p "$OUT"

exec "$PY" "$TOOL" "$REPO" \
  -f index -o "$OUT/map.index" \
  --cache "$OUT/map.graph.json" "$@"
