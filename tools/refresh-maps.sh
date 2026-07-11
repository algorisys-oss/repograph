#!/usr/bin/env bash
#
# refresh-maps.sh — refresh every repograph map under a directory, in one go.
#
# Walks BASE_DIR, finds each repo that already carries a .repograph/ map, and
# refreshes it via tools/refresh-map.sh (incremental thanks to --cache, so only
# changed files are re-analyzed). Because it only touches repos that already
# have a map, it skips anything you never set up — no accidental new maps.
#
# Usage:
#   tools/refresh-maps.sh [BASE_DIR] [--exclude PAT]... [-- extra repograph args]
#
#   tools/refresh-maps.sh /home/rajesh/opensource         # refresh all maps there
#   tools/refresh-maps.sh . --exclude vendor              # skip paths matching 'vendor'
#   tools/refresh-maps.sh ~/src -- --rebuild              # force full re-analysis
#
#   --exclude PAT   substring matched against each repo's path; repeatable.
#   everything after `--` is forwarded verbatim to each repograph run.
#
# Tool resolution and artifacts are identical to refresh-map.sh (which this
# calls); override the interpreter/binary with $REPOGRAPH / $REPOGRAPH_PY / $PYTHON.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REFRESH_ONE="$SCRIPT_DIR/refresh-map.sh"

# Optional first arg = base dir (must be an existing dir, not a flag).
BASE="$(pwd)"
if [ "${1:-}" ] && [ "${1#-}" = "${1:-}" ] && [ -d "${1:-}" ]; then
  BASE="$1"; shift
fi

# Parse --exclude patterns; collect pass-through args after `--`.
EXCLUDES=()
PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --exclude) EXCLUDES+=("${2:-}"); shift 2 ;;
    --exclude=*) EXCLUDES+=("${1#*=}"); shift ;;
    --) shift; PASS=("$@"); break ;;
    *) PASS+=("$1"); shift ;;
  esac
done

BASE="$(cd "$BASE" && pwd)"

# Find repos with an existing map (prune so we don't descend into .repograph).
mapfile -t REPOS < <(
  find "$BASE" -type d -name .repograph -prune 2>/dev/null \
    | sed 's|/\.repograph$||' | sort -u
)

if [ "${#REPOS[@]}" -eq 0 ]; then
  echo "refresh-maps: no .repograph/ maps found under $BASE" >&2
  exit 0
fi

ok=0; fail=0; skipped=0
for repo in "${REPOS[@]}"; do
  skip=""
  for pat in "${EXCLUDES[@]:-}"; do
    [ -n "$pat" ] && case "$repo" in *"$pat"*) skip=1 ;; esac
  done
  if [ -n "$skip" ]; then
    printf 'SKIP %s\n' "$repo"; skipped=$((skipped+1)); continue
  fi
  t0=$SECONDS
  if "$REFRESH_ONE" "$repo" ${PASS[@]+"${PASS[@]}"} >/dev/null 2>&1; then
    printf 'OK   %-40s %3ds  index=%s\n' "$repo" "$((SECONDS-t0))" \
      "$(du -h "$repo/.repograph/index.txt" 2>/dev/null | cut -f1)"
    ok=$((ok+1))
  else
    printf 'FAIL %-40s %3ds\n' "$repo" "$((SECONDS-t0))"
    fail=$((fail+1))
  fi
done

printf '\n%d refreshed, %d skipped, %d failed\n' "$ok" "$skipped" "$fail"
[ "$fail" -eq 0 ]
