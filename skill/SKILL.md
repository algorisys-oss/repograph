---
name: repograph
description: >-
  Build or refresh a compact, token-cheap repo map (file:line index of every
  file + its symbols) and use it to navigate a codebase instead of broad
  reading. TRIGGER when you need to orient in an unfamiliar or large repo,
  locate where a symbol/function/type is defined, or answer "where is X" /
  "what's in this codebase" questions — especially before grepping or reading
  many files. Routes you to the exact path:line so you open only the 2-3 files
  you actually need.
---

# repograph

A zero-dependency repo-map generator. It emits a terse one-line-per-file index
(`path | lang | lines | name:line …`) that is typically 100×+ smaller than the
source, so you read the index, jump to a `path:line`, and open only the files
you need instead of grepping and reading broadly.

## Install (one-time)

This skill drives the `repograph.py` CLI from the
[repograph](https://github.com/) repo (Python 3.8+, standard library only).

1. Clone the repo and copy this skill into your Claude skills dir:
   ```bash
   git clone <repo-url> ~/src/repograph
   mkdir -p ~/.claude/skills/repograph
   cp ~/src/repograph/skill/SKILL.md ~/.claude/skills/repograph/
   ```
2. Make the tool reachable. Either put `repograph.py` on your `PATH`, or set an
   env var the commands below use:
   ```bash
   export REPOGRAPH_PY="$HOME/src/repograph/repograph.py"
   ```
3. (Optional, recommended) install [universal-ctags](https://github.com/universal-ctags/ctags)
   for precise symbols incl. qualified methods: `apt-get install universal-ctags`
   / `brew install universal-ctags`. Without it, a built-in regex fallback is used.

> Note: some environments expose the interpreter as `python3`, others as
> `python`. Use whichever your system has.

## When to use

- Orienting in an unfamiliar or large repo before making changes.
- Answering "where is `<symbol>` defined?", "what files implement X?", "what's
  the shape of this codebase?".
- Any time you're tempted to grep across the whole tree or read many files to
  find something structural — build/read the index first.

## Workflow

1. **Pick the target repo.** Default to the current working directory (the repo
   you're in). The `repo` argument is optional and defaults to `.`.

2. **Refresh the index (incremental).** Keep a per-repo JSON cache so re-runs
   only re-analyze changed files. Use a cache file under a stable cache dir keyed
   by the repo's absolute path so it never pollutes the repo. Run from inside the
   target repo:

   ```bash
   REPOGRAPH_PY="${REPOGRAPH_PY:-repograph.py}"   # PATH name or full path
   REPO="$(pwd)"
   CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/repograph"
   mkdir -p "$CACHE_DIR"
   SLUG="$(printf '%s' "$REPO" | tr '/' '_' | sed 's/^_//')"
   python3 "$REPOGRAPH_PY" "$REPO" \
     -f index -o "$CACHE_DIR/$SLUG.index" --cache "$CACHE_DIR/$SLUG.json"
   ```

   On a warm cache it prints `Updated: N changed, M reused, K removed`. Use
   `--rebuild` to force a full re-analysis.

3. **Scope big repos** with repeatable globs (matched on repo-relative paths)
   so the index stays small and relevant:

   ```bash
   ... "$REPO" --include 'src/*' --include 'lib/*' --exclude '*test*'
   ```

4. **Read the index** (`$CACHE_DIR/$SLUG.index`). Each line is
   `path | lang | lines | symbol:line symbol:line …`. Methods are grouped under
   their owner as `Owner{member:line member:line …}` (the class prefix is written
   once), and a class that has methods shows as `Owner:line{member:line …}`. So
   `Server:1{get:8 put:12}` means class `Server` at line 1 with methods `get`
   (line 8) and `put` (line 12). Scan for the file or symbol you need.

5. **Jump to the source.** Open the specific `path` at the listed line with the
   Read tool (use the line as the `offset`). Read only what you need — the index
   already told you where it is. Avoid re-grepping for something the index lists.

## Formats (`-f`)

- `index` (default for this skill) — terse, smallest, best for routing.
- `md` — human-readable map with clickable `[name](path#Lnn)` links; good for
  pasting into a chat or reading yourself.
- `json` — structured graph (also the cache schema) for programmatic use.

## Symbol backend

Symbols come from **universal-ctags** when it's installed (precise; includes
methods, qualified as `Owner.method`), else from a **regex fallback** (zero-dep,
but misses JS/TS/Java methods and doesn't qualify). ctags is used automatically
when present; nothing extra to do. `--symbols full` adds members/fields (bigger
index); `--symbols none` drops symbols entirely. Imports/docs are always regex.

## Honest limits

- Without universal-ctags, the regex fallback catches only top-level-ish defs —
  class/object **methods are missed** for JS/TS/Java. Installing
  universal-ctags closes this.
- Imports/relationships are the weakest layer. The call graph (`--edges` and the
  `--callers`/`--callees`/`--impact`/`--affected` queries) is heuristic and
  name-based — no type resolution, so it's a navigation aid to verify, not ground
  truth. The tree-sitter backend makes it much more precise.
- Treat the index as a high-recall router, not ground truth: if a symbol isn't
  listed, fall back to a scoped grep in the file the index points you to.

## Quick lookups (CLI query modes)

Instead of reading the whole index, you can ask repograph directly (it builds
via `--cache` first, then prints just the answer):

```bash
python3 "$REPOGRAPH_PY" . --find build_repo         # where is a symbol DEFINED
python3 "$REPOGRAPH_PY" . --search "parse message"  # rank symbols by name/doc words
python3 "$REPOGRAPH_PY" . --refs handleClick        # where a name is USED (approx.)
```

- `--find` — exact/substring symbol definition lookup → `path:line kind name`.
- `--search` — lexical "by intent": matches query words against symbol names and
  file doc comments (not semantic — won't find concepts not named anywhere).
- `--refs` — lexical usages (git grep under the hood); name-based, so approximate
  (can't tell `a.connect()` from `b.connect()`). Use for "who references X".

## Relationship / call-graph queries

These build a call graph (`--edges` turns on automatically) so you can reason
about *connections*, not just locations. Approximate — name-based, so verify
before relying on them; far more precise with the tree-sitter backend below.

```bash
python3 "$REPOGRAPH_PY" . --callers build_repo    # who calls this?
python3 "$REPOGRAPH_PY" . --callees cmd_init       # what does this call?
python3 "$REPOGRAPH_PY" . --impact analyze_bytes   # blast radius + affected tests
python3 "$REPOGRAPH_PY" . --affected src/app.py    # files/tests depending on these
python3 "$REPOGRAPH_PY" . --affected               # …using git's changed files
```

- `--impact` is ideal before a risky change: it lists the transitive callers and
  flags which **test files** are affected.
- `--affected` with no args reads git's changed files — a quick "what to re-test".

## Precise backend (optional tree-sitter)

For accurate symbols **and** call edges (attributed to the exact enclosing scope
from a real AST, not regex), add `--tree-sitter` after a one-time
`pip install tree-sitter-language-pack`. Covers Python, JS/TS, Go, Rust, Java,
Ruby, C; other files fall back to ctags/regex. Without the wheel, `--tree-sitter`
just warns and falls back, so it's always safe to pass.

## Optional: MCP server

This skill drives the CLI directly — that's all you need. Optionally, the same
library is wrapped as a stdlib-only MCP server (`repograph_mcp.py` in the repo)
exposing eight tools — `repo_index`, `find_symbol`, `search`, `find_refs`,
`callers`, `callees`, `impact`, `affected` — for MCP clients (e.g. Google
Antigravity) or sessions where calling a tool beats shelling out. If the
`repograph` MCP tools are available, prefer `find_symbol` for "where is X
defined", `search` for by-intent, `find_refs` for "who uses X", `callers`/
`callees`/`impact` for the call graph, and `repo_index` for routing. See the
README's "Use as an MCP server" section.
