#!/usr/bin/env bash
#
# install-git-hook.sh — auto-refresh the repograph map on git activity.
#
# Installs (or appends to) post-commit, post-merge and post-checkout hooks that
# call refresh-map.sh, so the index in <repo>/.repograph/ stays current with no
# manual step. Existing hooks are preserved (our block is appended, not clobbered).
#
# Usage:
#   tools/install-git-hook.sh [REPO_DIR]
#   REPOGRAPH_PY=/path/to/repograph.py tools/install-git-hook.sh ~/proj
#
# Re-running is safe (idempotent). To uninstall, delete the marked block from the
# hook files in <repo>/.git/hooks/.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REFRESH="$SCRIPT_DIR/refresh-map.sh"
REPO="${1:-$(pwd)}"
RP="${REPOGRAPH_PY:-}"   # bake an explicit tool path into the hook if provided

GITDIR="$REPO/.git"
[ -e "$GITDIR" ] || { echo "not a git repo: $REPO" >&2; exit 1; }
# Support worktrees / submodules where .git is a file pointing elsewhere.
if [ -f "$GITDIR" ]; then
  GITDIR="$REPO/$(sed -n 's/^gitdir: //p' "$GITDIR")"
fi
HOOK_DIR="$GITDIR/hooks"
mkdir -p "$HOOK_DIR"

MARKER="# >>> repograph refresh-map hook >>>"
for h in post-commit post-merge post-checkout; do
  HOOK="$HOOK_DIR/$h"
  if [ -f "$HOOK" ] && grep -qF "$MARKER" "$HOOK"; then
    echo "$h: already installed — skipping"
    continue
  fi
  [ -f "$HOOK" ] || printf '#!/bin/sh\n' > "$HOOK"
  {
    echo "$MARKER"
    echo "REPOGRAPH_PY=\"$RP\" \"$REFRESH\" \"$REPO\" >/dev/null 2>&1 || true"
    echo "# <<< repograph refresh-map hook <<<"
  } >> "$HOOK"
  chmod +x "$HOOK"
  echo "$h: installed"
done

echo "Done. The map at $REPO/.repograph/ will refresh on commit / merge / checkout."
