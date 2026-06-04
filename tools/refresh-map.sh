#!/usr/bin/env bash
#
# refresh-map.sh — (re)build a repograph map for a project, incrementally.
#
# A manual, non-npm convenience: re-run any time; thanks to --cache only changed
# files are re-analyzed. Writes the canonical artifacts that `repograph init`
# and the npm integration use:
#   <repo>/.repograph/index.txt    (terse routing index)
#   <repo>/.repograph/map.md       (human-readable map)
#   <repo>/.repograph/graph.json   (cache)
#
# Usage:
#   tools/refresh-map.sh [REPO_DIR] [extra repograph args...]
#
#   tools/refresh-map.sh                       # map the current directory
#   tools/refresh-map.sh /path/to/project      # map another project
#   tools/refresh-map.sh . --include 'src/*'   # pass through any repograph flag
#   tools/refresh-map.sh . --rebuild           # force a full re-analysis
#
# Finding the repograph tool (in priority order):
#   1. $REPOGRAPH         — an executable (e.g. node_modules/.bin/repograph)
#   2. $REPOGRAPH_PY      — a repograph.py path, run via $PYTHON (default python3)
#   3. <repo>/node_modules/.bin/repograph   — the npm-installed bin, if present
#   4. ../repograph.py relative to this script  — the in-source case
#   5. `repograph` on $PATH
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

# Optional first arg = repo dir (must be an existing dir, not a flag).
REPO="$(pwd)"
if [ "${1:-}" ] && [ "${1#-}" = "${1:-}" ] && [ -d "${1:-}" ]; then
  REPO="$1"; shift
fi

# Resolve the tool. RG is an argv array so the python case can carry two words.
if [ -n "${REPOGRAPH:-}" ]; then
  RG=("$REPOGRAPH")
elif [ -n "${REPOGRAPH_PY:-}" ]; then
  RG=("$PY" "$REPOGRAPH_PY")
elif [ -x "$REPO/node_modules/.bin/repograph" ]; then
  RG=("$REPO/node_modules/.bin/repograph")
elif [ -f "$SCRIPT_DIR/../repograph.py" ]; then
  RG=("$PY" "$SCRIPT_DIR/../repograph.py")
elif command -v repograph >/dev/null 2>&1; then
  RG=(repograph)
else
  echo "refresh-map: cannot find repograph — set REPOGRAPH=/path/to/bin or REPOGRAPH_PY=/path/to/repograph.py" >&2
  exit 1
fi

OUT="$REPO/.repograph"
mkdir -p "$OUT"

# Two passes (index + md) sharing one cache, so the second is incremental.
"${RG[@]}" "$REPO" -f index -o "$OUT/index.txt" --cache "$OUT/graph.json" "$@"
exec "${RG[@]}" "$REPO" -f md -o "$OUT/map.md" --cache "$OUT/graph.json" "$@"
