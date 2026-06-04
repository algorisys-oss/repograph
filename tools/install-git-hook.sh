#!/usr/bin/env bash
#
# install-git-hook.sh — set up the committed-map workflow for a repo.
#
# Thin wrapper over `repograph --init`: it builds <repo>/.repograph/, writes a
# tracked .githooks/pre-commit that refreshes + stages the map on every commit,
# and activates it (core.hooksPath). The map then travels with the repo, and a
# fresh clone gets it immediately — refreshing only needs the tool, reading does
# not. Re-running is safe (idempotent: it rewrites the same managed files).
#
# Usage:
#   tools/install-git-hook.sh [REPO_DIR] [--include 'src/*' --exclude '*test*' ...]
#   REPOGRAPH=/path/to/repograph tools/install-git-hook.sh ~/proj
#
# Scope flags after REPO_DIR are forwarded to --init and baked into the hook.
#
# Finding the repograph tool (priority): $REPOGRAPH (executable) → $REPOGRAPH_PY
# (via $PYTHON) → <repo>/node_modules/.bin/repograph → ../repograph.py → PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

# Optional first arg = repo dir (must be an existing dir, not a flag).
REPO="$(pwd)"
if [ "${1:-}" ] && [ "${1#-}" = "${1:-}" ] && [ -d "${1:-}" ]; then
  REPO="$1"; shift
fi

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
  echo "install-git-hook: cannot find repograph — set REPOGRAPH=/path/to/bin or REPOGRAPH_PY=/path/to/repograph.py" >&2
  exit 1
fi

exec "${RG[@]}" "$REPO" --init "$@"
