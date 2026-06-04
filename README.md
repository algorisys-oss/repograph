# repograph

Point it at any local repo and it emits a Markdown **repo map** — a compact,
navigable knowledge graph of the codebase that's handy to feed an LLM (or read
yourself). Every file and symbol links back to the original source as
`path#Lnn`, so the map doubles as an index into the code.

- **Zero dependencies** — Python 3.8+ standard library only.
- **Language-agnostic** — a small extensible table of regexes per language; any
  file still gets a node with its line count.
- **Shallow & honest** — extraction is heuristic (regex, no real parsing). It
  will miss and mis-tag some things, especially imports. The output says so.

## Usage

```bash
# Clone/copy the repo you want to map first, then:
python repograph.py                               # map the current directory -> stdout
python repograph.py /path/to/repo                 # print Markdown to stdout
python repograph.py /path/to/repo -o REPOMAP.md   # write to a file

# Scope big repos with repeatable globs (matched against repo-relative paths):
python repograph.py /path/to/repo --include 'src/*' --include 'lib/std/*'
python repograph.py /path/to/repo --exclude 'lib/libc/*' --exclude '*test*'

# Pick an output format (see "Output formats" below):
python repograph.py /path/to/repo -f index -o REPOMAP.index   # terse, smallest
python repograph.py /path/to/repo -f json  -o graph.json      # machine-readable

# Incremental update: keep a JSON cache; only changed files are re-analyzed:
python repograph.py /path/to/repo -o REPOMAP.md --cache .repograph.json

# Symbol detail vs. tokens (see "Symbol extraction"): none | defs (default) | full
python repograph.py /path/to/repo -f index --symbols full     # incl. members/fields
python repograph.py /path/to/repo -f index --no-ctags         # force regex backend
```

Links in the Markdown output are relative to the repo root, so they resolve when
the `REPOMAP.md` lives at the repo root (or you read it from there).

## Output formats (`-f`)

| Format  | What it is | Use it for |
|---------|------------|------------|
| `md` (default) | Human-readable map: overview, tree, per-file docs + clickable `[name](path#Lnn)` symbol links + imports | reading; pasting into a chat |
| `index` | One terse line per file: `path \| lang \| lines \| name:line …`; methods collapse under their owner as `Owner{m:line …}` so the prefix is written once — no docs/links | cheapest LLM routing; ~4× smaller than `md` |
| `json`  | Compact structured graph (nodes/edges); also the cache schema | feeding tools/agents programmatically |

## Use as an MCP server (optional)

repograph is a CLI first — this is entirely optional. For agent clients that
prefer calling tools over running shell commands, `repograph_mcp.py` wraps the
same library as a [Model Context Protocol](https://modelcontextprotocol.io)
server. It is **stdlib-only too** (no `pip install`, no extra deps) and imports
repograph in-process, so it runs anywhere `python3` does and works in any MCP
client (Claude Code, Google Antigravity, Cursor, …). Delete the file and nothing
else changes.

It exposes two tools:

| Tool | What it does |
|------|--------------|
| `repo_index` | Build/refresh the index (cached, incremental) and return it for routing. Args: `path?`, `include?`, `exclude?`, `symbols?`, `no_ctags?`, `rebuild?` |
| `find_symbol` | Find where a symbol is defined → `path:line  kind  name` rows (exact matches first; finds qualified `Owner.method`). Args: `query`, `path?`, `kind?`, `limit?` |

Register it by pointing any MCP client at `python3 .../repograph_mcp.py`:

```jsonc
// Claude Code: ~/.claude.json or a project .mcp.json
// Antigravity: its MCP settings (same schema)
{
  "mcpServers": {
    "repograph": {
      "command": "python3",
      "args": ["/path/to/repograph/repograph_mcp.py"]
    }
  }
}
```

For Claude Code you can also run:
`claude mcp add repograph -- python3 /path/to/repograph/repograph_mcp.py`.

The server reuses the same per-repo cache under `~/.claude/repograph-cache/`, so
the first call on a big repo does the full pass and later calls are fast.

## Use as a Claude skill (optional)

[`skill/SKILL.md`](skill/SKILL.md) packages the "refresh index → read it → jump
to `path:line`" workflow as a [Claude Code](https://docs.claude.com/en/docs/claude-code)
skill, so the agent reaches for repograph automatically when you ask "where is
X?" / "what's in this repo?". Install it:

```bash
mkdir -p ~/.claude/skills/repograph
cp skill/SKILL.md ~/.claude/skills/repograph/
# point the skill at this checkout (or put repograph.py on your PATH):
export REPOGRAPH_PY="$PWD/repograph.py"
```

It's optional and self-contained — it just drives the CLI described above.

## Incremental updates

Pass `--cache FILE` to keep a JSON snapshot of the graph. On the next run,
repograph reads each file, hashes its bytes, and **reuses unchanged files
verbatim** (skipping the regex work); only changed files are re-analyzed and
deleted files are dropped. It prints `Updated: N changed, M reused, K removed`.
Use `--rebuild` to ignore the cache and analyze everything.

Change detection is by **content hash**, so it's robust to fresh clones and
mtime quirks (the trade-off: every file is still read each run to hash it — the
saved work is the parsing, not the I/O).

### Keeping the map fresh in your own project

Two helper scripts live in [`tools/`](tools/):

- [`tools/refresh-map.sh`](tools/refresh-map.sh) — incrementally (re)build the
  index + cache into `<repo>/.repograph/`. Re-run it any time; only changed files
  are re-analyzed.
  ```bash
  tools/refresh-map.sh                     # map the current project
  tools/refresh-map.sh /path/to/project    # or another one
  tools/refresh-map.sh . --include 'src/*' # any repograph flag passes through
  ```
  It finds `repograph.py` via `$REPOGRAPH_PY`, then a sibling `../repograph.py`,
  then `PATH` — so set `REPOGRAPH_PY` if you copy the script out of this repo.

- [`tools/install-git-hook.sh`](tools/install-git-hook.sh) — install
  `post-commit` / `post-merge` / `post-checkout` hooks that run `refresh-map.sh`,
  so the map updates automatically as the code changes (existing hooks are
  preserved, not overwritten):
  ```bash
  tools/install-git-hook.sh /path/to/project
  ```

Either commit `.repograph/` (warm, consistent maps for teammates/CI) or add it to
`.gitignore` (each clone rebuilds once). Agent integrations (the Claude skill and
the MCP tools) refresh the cache on every call, so they're never stale.

## What's in the map

- **Overview** — file/line/symbol counts, language breakdown, README blurb.
- **Tree** — the directory outline.
- **Files** — per file: language, line count, leading doc comment/docstring,
  the symbols it defines (each a `path#Lnn` link), and its imports (heuristic).

### Data model (the "graph")

- **Nodes:** directories, files (path, language, line count), symbols
  (name, kind, line).
- **Edges:** *contains* (dir→file, file→symbol) and *imports* (file→module,
  heuristic).

## Token efficiency

The whole point is to be much smaller than the code while still routing an LLM to
the right `file:line`. Measured on the ziglang/zig standard library + compiler
(`lib/std` + `src`, 746 files, with the default ctags `--symbols=defs`):

| Artifact | Size | ~Tokens | vs. reading the source |
|----------|------|---------|------------------------|
| Source in scope | 42.2 MB | ~10.6M | 1× |
| `md` map (compact) | 1.66 MB | ~415k | **26× smaller** |
| `json` graph | 1.53 MB | ~381k | **28× smaller** |
| `index` (terse) | 0.29 MB | ~73k | **145× smaller** |

So instead of an agent reading ~10.6M tokens of source to find things, it reads
a ~73k-token index, jumps to the exact `path:line`, and opens only the 2–3 files
it needs. Slice further with `--include` to shrink any of these. (The terse index
is even smaller than the pre-ctags ~111k figure here: ctags groups and qualifies
methods, which the grouped `Owner{m:line …}` rendering then packs tightly.)

The ctags backend with the default `--symbols=defs` doesn't undo this — it can
*improve* it. On the Python `PIL` package (~1.14 MB source, ~286k tokens), the
`index`:

| Backend (`--symbols=defs`) | Size | ~Tokens | vs. source |
|----------------------------|------|---------|------------|
| regex (`--no-ctags`) | 23.4 KB | ~5.8k | 49× smaller |
| **universal-ctags** | **14.2 KB** | **~3.5k** | **80× smaller** |

ctags is *smaller here* despite being more precise: the regex backend emits every
method ungrouped/unqualified, while ctags groups them under their class
(`Image{save:… load:…}`) and qualifies them. (On JS/Java-heavy code ctags-`defs`
runs slightly *larger* than regex — but only because it surfaces methods regex
misses entirely. `--symbols=full` is always bigger; it adds members/fields.)

## Supported languages

Python, JavaScript/TypeScript, Zig, C/C++, Go, Rust, Ruby, Java, Shell, Elixir.
Adding one is a few lines in the `LANGS` table in
[repograph.py](repograph.py) — give it the file extensions, a few symbol regexes
(each capturing the name), and a few import regexes. Unknown extensions still
appear in the tree with their line counts.

## Symbol extraction (ctags fast-path + regex fallback)

Symbols are extracted one of two ways, decided automatically per run:

- **universal-ctags** — if a [universal-ctags](https://github.com/universal-ctags/ctags)
  binary is on `PATH`, repograph uses it for precise symbols (with kinds and
  scope) across ~150 languages. This is what surfaces **class/object methods**
  (which the regex backend misses for JS/TS/Java) and qualifies them as
  `Owner.method`. Install it with e.g. `apt-get install universal-ctags` /
  `brew install universal-ctags`. Pass `--no-ctags` to force the regex backend,
  or `--ctags-path BIN` to point at a specific binary. (Exuberant Ctags is not
  supported — it lacks JSON output.)
- **regex fallback** — when ctags is absent (or can't parse a file), the
  built-in zero-dependency regex extractor is used, exactly as before. ctags is
  an *optional* binary; nothing is required to install.

Either way, **imports and leading docs are always regex** (ctags doesn't model
them). ctags runs once per build, only over changed files, and never on
cache-reused files.

### `--symbols` — detail vs. tokens

Because ctags surfaces *far* more symbols (every method and member), a knob
trades completeness against index size:

| Level | What it includes | Size |
|-------|------------------|------|
| `none` | no symbols — just `path \| lang \| lines` (+imports in `md`) | smallest |
| `defs` (default) | top-level defs **+ qualified methods**; drops members/fields | ≈ today's size, now with methods |
| `full` | everything incl. members, fields, variables, constants | largest |

The default (`defs`) plus the grouped index rendering (`Owner{m:line …}`) keeps
the token win intact while fixing the missing-methods gap.

## How it walks a repo

- If the directory is a git repo, it uses `git ls-files` (so `.gitignore` is
  respected). Otherwise it walks the tree, skipping common noise dirs
  (`.git`, `node_modules`, `__pycache__`, `target`, `.zig-cache`, …).
- Binary files (NUL-byte sniff) and files over ~2 MB are skipped.

## Tests

```bash
python -m unittest discover -s tests
```

The suite builds a small temp fixture repo across a few languages and asserts
the extracted symbols land on the **correct line numbers**, that noise dirs are
skipped, and that the include/exclude filters work.

## Example

Map a large repo — e.g. the [ziglang/zig](https://github.com/ziglang/zig)
standard library and compiler (`lib/std` + `src`) — scoped with `--include` and
cached for fast re-runs:

```bash
python repograph.py /path/to/ziglang/zig \
  --include 'lib/std/*' --include 'src/*' \
  -o zig.repomap.md \
  --cache zig.graph.json
```

## Limitations & upgrade path

- **Symbols:** with universal-ctags installed, symbols are precise (incl.
  methods, qualified). Without it, the **regex fallback** is approximate — it
  catches top-level-ish definitions but misses JS/TS/Java methods and doesn't
  qualify names. Install universal-ctags to close that gap (see *Symbol
  extraction* above).
- **Imports/relationships are always regex** and remain the weakest part — and
  there's still no call graph or type resolution (a much harder, research-grade
  problem). tree-sitter grammars would be the next step there, at the cost of
  the zero-dependency simplicity.

## License

[MIT](LICENSE) © 2026 Rajesh Pillai
