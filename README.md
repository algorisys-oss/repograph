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

## Install

The tool is the single file `repograph.py` — there is nothing to build. Pick
whichever fits your project:

```bash
# A) Run it directly (needs Python 3.8+ on PATH; nothing else):
python repograph.py --help

# B) Install as an npm CLI — handy when a JS/TS repo wants to depend on it.
#    This adds a `repograph` command that forwards to the bundled Python script,
#    so Python 3.8+ must still be on PATH. No npm-registry publish needed:
npm install -D github:algorisys-oss/repograph            # latest on the default branch
npm install -D github:algorisys-oss/repograph#v0.1.0     # pin to a tag (recommended)
npm install -D github:algorisys-oss/repograph#<commit>   # pin to a commit SHA

# Then run it via the linked bin (or `npx repograph`):
npx repograph --help
```

When installed via npm, the `repograph` command is a thin Node shim
(`bin/repograph.js`) that spawns `python3 repograph.py` (falling back to
`python`) with your args. Override the interpreter with the `PYTHON` env var
(e.g. `PYTHON=python`). Everywhere the examples below show `python repograph.py`,
you can substitute `repograph` / `npx repograph` once installed.

### Keep the map fresh automatically (`repograph init`)

For a repo that should carry a committed, always-current map, run once:

```bash
npx repograph init                              # whole repo
npx repograph init --include 'src/*' --exclude '*test*'   # scoped (baked into the hook)
```

`init` writes `.repograph/{index.txt,map.md,graph.json}`, installs a tracked
`.githooks/pre-commit` that refreshes the map and `git add`s it on every commit,
and points `core.hooksPath` at `.githooks`. The map is then **committed with your
code**, so a fresh clone has it immediately — reading the map needs no Python at
all; only refreshing does. The hook resolves the tool through the consumer's own
`node_modules/.bin/repograph`, so it works in any repo that depends on repograph,
and no-ops cleanly when the tool isn't installed (e.g. before `npm install`).

To make the hook auto-activate for everyone who clones, add to `package.json`:

```json
"scripts": { "prepare": "git config core.hooksPath .githooks" }
```

Commit `.repograph/` and `.githooks/`. (Without npm, `tools/install-git-hook.sh`
does the same via the in-source tool.)

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

# Query modes — print a lookup instead of a map (build via --cache for speed):
python repograph.py . --find build_repo        # where is a symbol defined?
python repograph.py . --search "parse message" # rank symbols by name/doc words
python repograph.py . --refs run_ctags         # where is a name used? (approx.)

# Relationship / call-graph queries (auto-build a resolved call graph):
python repograph.py . --callers build_repo     # who calls this?
python repograph.py . --callers build_repo --strict   # high-confidence edges only
python repograph.py . --callees cmd_init        # what does this call?
python repograph.py . --impact analyze_bytes    # blast radius (transitive callers)
python repograph.py . --affected repograph.py   # which files/tests depend on these?
python repograph.py . --affected                # …using git's changed files

# Read the SOURCE in one call (no follow-up file open needed):
python repograph.py . --node find_impact        # a symbol's source + caller/callee trail
python repograph.py . --explore "resolve call edges"  # relevant symbols' source + call paths

# Persist relationship edges (calls/extends/implements) into the map + json:
python repograph.py /path/to/repo --edges -f md -o REPOMAP.md

# Optional tree-sitter backend — precise symbols + call edges (real ASTs):
pip install tree-sitter-language-pack            # one-time, optional
python repograph.py /path/to/repo --tree-sitter --edges -f index

# Watch mode — rebuild incrementally on every change (Ctrl-C to stop):
python repograph.py /path/to/repo -o REPOMAP.md --cache .repograph.json --watch
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

It exposes these tools:

| Tool | What it does |
|------|--------------|
| `repo_index` | Build/refresh the index (cached, incremental) and return it for routing. Args: `path?`, `include?`, `exclude?`, `symbols?`, `no_ctags?`, `tree_sitter?`, `edges?`, `rebuild?` |
| `find_symbol` | Find where a symbol is **defined** → `path:line  kind  name` rows (exact matches first; finds qualified `Owner.method`). Args: `query`, `path?`, `kind?`, `limit?` |
| `search` | Lexical **by-intent** symbol search — ranks by word-token overlap of the query with symbol names *and* file doc comments (`retry request` → `retryRequest`). Not semantic. Args: `query`, `path?`, `limit?` |
| `find_refs` | Find where an identifier is **used** → `path:line [def] text` (git grep; approximate, name-based). Args: `name`, `path?`, `limit?` |
| `callers` | **Who calls** a function/method → `path:line in <enclosing symbol>` (heuristic call graph; precise with `tree_sitter`). Args: `name`, `path?`, `limit?` |
| `callees` | **What a symbol calls** → `callee -> def path:line` (external/unresolved flagged). Args: `name`, `path?`, `limit?` |
| `impact` | **Blast radius** of changing a symbol — its transitive callers, with affected test files called out. Args: `name`, `path?`, `max_depth?` |
| `affected` | Given **changed files**, the files/tests that depend on them (tests first). Args: `files?` (array or comma string; omit for git changes), `path?` |
| `node` | **Read a symbol without a follow-up file open** → its verbatim line-numbered **source** + caller/callee trail (confidence-tagged). Args: `name`, `path?`, `max_defs?`, `min_confidence?`/`strict?` |
| `explore` | **Understand an area in one call** → the relevant symbols' **source** plus the call paths *between* them, from a set of names or a question. Args: `query`, `path?`, `max_files?`, `min_confidence?`/`strict?` |

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

The cache is **profile-aware**: each entry records the `--symbols` level and
whether ctags produced it, so changing `--symbols` (or installing/removing
ctags) automatically re-analyzes the affected files instead of serving stale
symbols — no `--rebuild` needed.

Change detection is by **content hash**, so it's robust to fresh clones and
mtime quirks. In a **git repo** there's a fast path: repograph compares each
tracked file's git blob sha (from `git ls-files -s`, no file read) against the
cache and only reads files git reports as changed or dirty — so warm re-runs on
large repos skip the per-file hashing I/O too, while non-git repos fall back to
hashing every file (always correct).

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
  (name, kind, line, and — with `--edges` — end line).
- **Edges:** *contains* (dir→file, file→symbol), *imports* (file→module,
  heuristic), and — with `--edges` — *calls* / *extends* / *implements*
  (symbol→name, resolved to definitions at query time).

## Relationships & the call graph (`--edges`)

By default the map is a flat index of *where things are*. Pass `--edges` (or use
any of the relationship queries, which turn it on automatically) to also extract
*how things connect*:

- **`calls`** — within each definition's body, the names it calls. Attributed to
  the enclosing symbol so the graph is per-function, not per-file.
- **`extends` / `implements`** — class/type inheritance from declaration headers.

These power these queries, mirroring what a heavier code-intelligence tool gives
you, but kept honest about being approximate:

| Query | Question it answers |
|-------|---------------------|
| `--callers NAME` | Who calls `NAME`? |
| `--callees NAME` | What does `NAME` call? (definitions resolved; external calls flagged) |
| `--impact NAME` | If I change `NAME`, what's the blast radius? (transitive callers, **affected tests flagged**) |
| `--affected FILES` | Which files/tests depend on these changed files? (omit `FILES` to use git's changed set) |
| `--node NAME` | Show me `NAME`'s **source** + its caller/callee trail (no follow-up file open) |
| `--explore QUERY` | Show me the **source** of the symbols relevant to `QUERY` + the call paths between them |

The last two return code, not just locations. Where `--callers`/`--find` route
you to a `path:line` you then have to open, **`--node`** and **`--explore`** hand
back the verbatim, line-numbered source (bodies capped ~80 lines, with an elision
note) plus the surrounding calls — so an agent can answer "what does this do / how
does it connect" in a single call. `--node` is one symbol and its trail;
`--explore` takes names *or* a question, gathers the most relevant symbols
(exact-name + lexical ranking, capped by `--max-files`), and shows the call edges
that run *between* them. Both honor `--strict`/`--min-confidence` to drop
low-confidence call-noise from the trail.

Edges are stored **name-based and unresolved per file** (so the incremental cache
stays correct — a file's edges depend only on that file), then **resolved at query
time** against the whole repo, each with a **confidence level**:

| Confidence | How it resolved |
|------------|-----------------|
| **high** | `self`/`super`/`this` → the method on the enclosing class **or its bases**; a receiver that's a known class; a receiver **variable** whose type is known from a `x = Foo()` assignment in scope; a free call to a **same-file** definition; or an **imported** call resolved to its defining file — a free import (Python/JS-TS `helper()`) or a package/class-qualified one (Go `util.Func()`, Java `Util.method()`) |
| **medium** | global name match, and the name is defined **exactly once** in the repo |
| **low** | global name match, but the name is **ambiguous** (defined in several places — best guess shown) |
| *external* | no definition in the repo (library/builtin call) |

So `self.run()` binds to *this* class's `run` (or the base it inherits from),
`helper()` imported from `./utils` binds to that file's `helper`, and Go
`util.Func()` / Java `Util.method()` bind through the import to the right package
or class file — not every same-named symbol. Filter with **`--strict`** (high
only) or **`--min-confidence {low,medium,high}`**; rows print their level
(`[medium]`, `[low]`).

Resolution quality scales with the backend: it's best on **tree-sitter** (exact
receivers + enclosing scope from a real AST), good on **ctags** (qualified
methods), and falls back to mostly medium/low on the pure-regex path. It's still a
navigation aid, not a type-checked ground truth — there's no generics/overload
resolution — but at **high** confidence it's reliable enough to act on.

## Backends: regex → ctags → tree-sitter

Symbols (and, with `--edges`, call edges) come from one of three backends, in
precedence order. The default stays **zero-dependency**; the better backends are
strictly opt-in or auto-detected, and any file a backend can't handle falls back
to regex.

| Backend | How it's selected | What it gives you |
|---------|-------------------|-------------------|
| **regex** (built-in) | always available; the floor | top-level defs + heuristic `name(` call edges; zero deps |
| **universal-ctags** | auto, when `ctags` is on `PATH` (unless `--no-ctags`) | precise symbols + qualified methods across ~150 languages; exact symbol **end lines** |
| **tree-sitter** | opt-in with `--tree-sitter` | real ASTs: precise symbols **and** call edges attributed to the exact enclosing scope (Python, JS/TS, Go, Rust, Java, Ruby, C) |

Enable tree-sitter with a one-time `pip install tree-sitter-language-pack` (or
`tree-sitter-languages`); without it, `--tree-sitter` warns and falls back. The
cache is **backend- and edges-aware** (each entry records `<level>:<backend>:<edges>`),
so switching backends or toggling `--edges` re-analyzes only what's affected.

## Watch mode (`--watch`)

`--watch` rebuilds the map incrementally whenever a file changes and rewrites the
output + cache, until you Ctrl-C. It polls file mtimes (no third-party watcher, so
the zero-dependency promise holds) at `--interval` seconds (default 1.0). Pair it
with `-o` and `--cache` to keep a committed map continuously fresh while you work.

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

> For a fuller reference — how repograph invokes ctags, using ctags directly,
> its use cases and limits — see [docs/ctags.md](docs/ctags.md).

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

- If the directory is a git repo, it uses
  `git ls-files --cached --others --exclude-standard` — i.e. tracked **and**
  untracked files, while honoring `.gitignore` (so a new, not-yet-committed file
  shows up, but ignored ones don't). Otherwise it walks the tree, skipping
  common noise dirs (`.git`, `node_modules`, `__pycache__`, `target`,
  `.zig-cache`, …).
- repograph's own `.repograph/` output dir is always skipped (it never indexes
  the map/cache it just wrote).
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
- **Call graph:** `--edges` + the relationship queries now build a **resolved**
  call graph — `self`/`super` calls bind through the class hierarchy, and imported
  calls bind to their defining file (Python & JS/TS free imports, Go package
  imports, Java class imports), each with a **confidence** level (use `--strict`
  to keep only high). Receiver variables are tracked through simple
  `x = Foo()` / `new Foo()` / `Foo{}` assignments so `x.run()` binds to `Foo.run`.
  What remains *unresolved* (medium/low confidence) is genuine ambiguity: no full
  type inference (factory returns, reassignment, generics/overloads), and no
  framework dynamic-dispatch synthesis. Quality is best on the tree-sitter backend.
  Treat low-confidence edges as hints; high-confidence ones are reliable.
- True **semantic** search (embeddings) is deliberately left out to keep the
  default zero-dependency; compose with an external index if you need it.

## License

[MIT](LICENSE) © 2026 Rajesh Pillai
