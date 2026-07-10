# repograph — session handoff / working context

This captures the design conversation and decisions behind the current code, so
work can continue in this folder without the original chat. For *usage*, see
[README.md](README.md); this file is the *why* and the *what's next*.

## What this tool is

A practical, **language-agnostic repo knowledge-graph / repo-map** generator.
Point it at any local repo (cloned/copied first) and it emits a compact map an
LLM can use to navigate the code — every file and symbol links back to
`path:line` so the map is an index into the source.

It is **not** a learning exercise — it's a utility wanted soon. (It started life
inside a Zig-from-first-principles learning project, then moved here to
`ai-tools/` as a standalone tool. It has no dependency on that project.)

## Status: working, 45 tests passing

```bash
python repograph.py <repo> -f index -o MAP        # run
python -m unittest discover -s tests              # test
```

Implemented end to end:
- Walk repo (git ls-files when available, else os.walk skipping noise dirs;
  binary + >2MB files skipped).
- Per-file: language (by extension), line count, leading doc/docstring, symbols
  (name/kind/line), imports — all via **regex heuristics**, no real parsing.
- Three output formats (`-f`): `md` (readable, clickable links), `json`
  (compact, agent-consumable), `index` (terse one-line-per-file, smallest).
- **Incremental update** via `--cache FILE`: a JSON snapshot; on re-run, files
  whose **content hash** is unchanged are reused (parsing skipped), changed ones
  re-analyzed, deleted ones dropped. `--rebuild` forces a full pass.
- `--include` / `--exclude` repeatable globs to scope big repos.

Everything lives in one file, `repograph.py` (~360 lines), stdlib only,
Python 3.8+. The `LANGS` table + shared `JS_TS_*` pattern lists are the only
place to touch when adding/improving a language.

## Decisions made (and why)

- **Python, stdlib only** — chosen for "soon" + zero install; the original fork
  was Python vs Zig vs shell+ctags.
- **Claude writes this tool** (unlike the learning project where the human writes
  every line) — it's a utility, not a lesson.
- **Language-agnostic + shallow (regex) by default** — chosen to keep the
  *default* zero-dependency and working on any repo. Accepted cost: approximate
  symbols, and imports/relationships are the weakest layer. The accuracy upgrade
  path is now built as **optional, opt-in backends** (see entry 5 below):
  universal-ctags (auto) and tree-sitter (`--tree-sitter`); the regex floor and
  zero-dep default are unchanged.
- **Content-hash change detection** (not mtime/size) — robust to fresh clones and
  touches. Cost: every file is still read each run to hash it, so warm runs save
  *parsing*, not *I/O* (modest speedup; always correct).
- **Compact rendering** — dropped the redundant trailing `— path:line` that
  duplicated each symbol's link. JSON is emitted compact (no indent).
- **JSON does double duty** — it's both the agent-facing artifact and the
  incremental cache (same schema).

## Measured token savings (ziglang/zig `lib/std` + `src`)

| Artifact | Size | ~Tokens | vs reading source |
|---|---|---|---|
| Source in scope | 40.3 MB | ~10.5M | 1× |
| `md` (compact) | 1.6 MB | ~413k | 26× smaller |
| `json` | 1.4 MB | ~380k | 28× smaller |
| `index` (terse) | 0.4 MB | ~111k | **95× smaller** |

Verdict: the token goal is met — an agent reads the ~111k-token index, jumps to
`path:line`, opens only the few files it needs, instead of ~10.5M tokens of
source. `--include` shrinks any of these further.

## Languages

Python, JavaScript, **TypeScript** (separate label; `.ts/.tsx`), Zig, C/C++,
Go, Rust, Ruby, Java, Shell, **Elixir**.

JS/TS extraction was hardened for modern stacks (React/Solid/Node): the
arrow-function pattern now catches **typed arrow consts**
(`const X: Component<P> = (props) => …`), typed async arrows with return types,
generics, single-arg arrows, and TS `enum`. Verified on a fixture covering
`.tsx/.jsx/.ts/.js/.ex`.

### Symbol backend: ctags fast-path + regex fallback (2026-06-04)
Symbols now come from **universal-ctags when present** (precise; methods
included and qualified as `Owner.method`; ~150 langs), else the original
**regex** extractor. Auto-on when the binary is on PATH; `--no-ctags` forces
regex, `--ctags-path BIN` overrides the binary. ctags runs once per build, only
over changed files, never on cache-reused files. **Imports + leading docs stay
regex always** (ctags doesn't model them). Detection rejects Exuberant Ctags
(no JSON). **Kind nuance:** some languages (Python, …) tag class methods as
kind `member` (not `method`); `run_ctags` promotes a `member` to `method` (so it's
qualified + kept at `defs`) only when its `scopeKind` is an OO container
(class/interface/trait/module/object — see `_CLASS_SCOPE_KINDS`), so C/struct
data fields (scopeKind `struct`) correctly stay fields. Implemented in
`ctags_available` / `run_ctags` / `_qualify`; wired
through `analyze_bytes`→`process_file`→`build_repo` (two-phase: hash/triage, then
one batch ctags call). universal-ctags **is installed** now (5.9.0) → it's the
live backend; the regex path is covered by stubbed tests + verified by forcing
`--no-ctags`.

**Token guardrails** (the reduced-tokens goal): `--symbols {none,defs,full}`,
default `defs` (top-level defs + qualified methods, drops members/fields); the
index groups methods under their owner as `Owner{m:line …}` so the class prefix
is written once; per-file symbol cap `MAX_SYMBOLS_PER_FILE` (80) with `+N more`.
Net: the methods gap is fixed while the index stays ~flat in size.

### Review fixes (2026-06-04)
Three reviewed defects fixed: (1) **cache profile-awareness** — `FileNode.build_key`
= `"<symbols_level>:<ctags?>"`, checked on both reuse branches in `build_repo`, so
changing `--symbols`/backend re-analyzes instead of serving stale symbols
(serialized as `bk`); (2) `.repograph` added to `NOISE_DIRS` **and** filtered in
`list_files` so repograph never indexes its own output; (3) `list_files` now uses
`git ls-files --cached --others --exclude-standard` to include untracked,
non-ignored files (still respects `.gitignore`).

### git-status fast path (2026-06-04)
In a git repo, `build_repo` reuses unchanged tracked files **without reading
them**: `git_blob_shas` (from `git ls-files -s`) gives each file's blob sha, and
a tracked, non-dirty file (`git_dirty_files` via `git status --porcelain`) whose
cached `gitsha` matches is reused as-is. New `gitsha` field on `FileNode`
(serialized only when present). Non-git repos fall back to read+hash. So warm
runs on large repos skip the per-file hashing I/O, not just parsing.

### Query layer (2026-06-04)
Library funcs in `repograph.py`: `find_symbol` (def lookup), `search_symbols`
(lexical "by intent" — `tokenize` splits camelCase/snake, ranks by name+doc word
overlap; NOT embeddings), `find_refs` (lexical usages via git grep, else stdlib
scan; approximate, flags def sites). Exposed as CLI `--find/--search/--refs` and
as MCP tools `find_symbol`/`search`/`find_refs` (repograph_mcp.py reuses the same
funcs). These widen usefulness toward "who calls this" / "by intent" without
adding deps; honest limits below.

### Honest limits (still)
- Without universal-ctags, the **regex fallback** misses JS/TS/Java methods and
  doesn't qualify names (install ctags to fix). CJS `module.exports = {…}` names
  still aren't pulled out by the regex path.
- `.vue/.svelte/.heex/.eex` templates get a file node but no symbols.
- Imports are always regex (weakest layer). There is now a **call graph**
  (`--edges` + `--callers`/`--callees`/`--impact`/`--affected`), but it's
  **heuristic and name-based** unless the tree-sitter backend is on: it can't
  tell `a.connect()` from `b.connect()` and there's still no type resolution.
  `find_refs`/`search` remain lexical. The `--tree-sitter` backend makes the call
  graph precise (exact enclosing scope); **embeddings** (true semantic) remain
  deliberately out to keep the default zero-dep.

## Open threads / next steps

1. **examples/ cleanup — DONE** (2026-06-04). User deleted the regenerable zig
   output; empty `examples/` dir removed; README example section de-linked.
   **Published:** repo is on GitHub at `algorisys-oss/repograph` (MIT, branch
   `main`); `tools/refresh-map.sh` + `tools/install-git-hook.sh` keep a project's
   map fresh; `tools/token-bench.py` measures the symbol-lookup token win.
2. **Claude skill — BUILT** (2026-06-04). Personal skill at
   `~/.claude/skills/repograph/SKILL.md`. Workflow: default to cwd → refresh a
   per-repo `--cache` index under `~/.claude/repograph-cache/<slug>.{json,index}`
   (slug = abs path with `/`→`_`, so it never pollutes the repo) → read the
   terse `index` → jump to `path:line`. Verified end to end (cold write + warm
   reuse). Enabled by a small CLI change: `repo` is now optional, defaulting to
   `.` (current directory).
3. **`gorepo-lens` — compared (2026-06-04), low overlap.** It's a *Go-specific,
   semantic/retrieval* tool that explicitly **composes with Semble**
   (embeddings + BM25) and adds only a Go structural layer; repograph is
   *language-agnostic, zero-dependency, regex-structural* mapping. Different
   axes (map vs. search) — keep both; no reason to fold one into the other.
4. **Accuracy upgrades — ctags + git fast path + query layer DONE** (2026-06-04):
   universal-ctags backend (qualified methods); git-status fast path; lexical
   `find_symbol`/`search`/`find_refs`.
5. **Relationship edges + tree-sitter backend + call-graph queries + watch —
   DONE** (2026-06-30, branch `feat/semantic-edges-tree-sitter`, merged to `dev`):
   - **`--edges`**: per-file `calls` (span-attributed to the nearest enclosing
     def), `extends`, `implements`; edges stay name-based/unresolved so the
     incremental cache stays per-file-correct. `Symbol` gains an `end` span;
     ctags now reads end lines too (`--fields +e`).
   - **Call-graph queries**: `--callers`, `--callees`, `--impact` (transitive
     callers + flagged affected tests), `--affected` (files/tests depending on a
     changed set; git's changed files when no arg). Mirrored as MCP tools.
   - **`--tree-sitter`** (opt-in, needs the `tree-sitter-language-pack` wheel):
     real ASTs for Python/JS/TS/Go/Rust/Java/Ruby/C → precise qualified symbols,
     exact spans, and call edges bound to the exact enclosing scope. Backend
     precedence tree-sitter > ctags > regex; binding-agnostic shim handles both
     property- and method-style tree-sitter wheels; falls back cleanly if absent.
   - **`--watch`**: stdlib mtime-poll incremental rebuild loop (`--interval`).
   - `build_key` is now `<level>:<backend>:<edges>` so switching backend or
     toggling edges re-analyzes only what's affected. 18 new tests; 69 pass.
6. **Resolved edges + confidence (Phase 1+2) — DONE** (2026-06-30, branch
   `feat/resolved-edges`): turns the name-based call graph into a *resolved* one.
   - **Receiver capture**: `Edge` gains `recv` (self/this/super or a var); both
     tree-sitter (`_ts_call_target`) and the regex `extract_calls` record it.
   - **`resolve_edges(repo)`** — a separate in-memory pass (NOT cached, to keep
     per-file edges incremental-correct) that binds each call to a concrete def
     with a confidence: **high** = self/super/typed-receiver via the class
     hierarchy (`class_registry` + `_lookup_method`), same-file local def, or an
     **import**-resolved target; **medium** = unique global name; **low** =
     ambiguous; *external* = not in repo.
   - **Import resolution (Phase 2)**: `extract_import_bindings` records
     `local_name → module` (Python `from … import …`; JS/TS `import {…}`/default/
     namespace); `_resolve_py_module` / `_resolve_js_module` map a module string
     to a repo file (relative levels, extension + index resolution). Persisted as
     `ibind` under `--edges`; `build_key` edge tag bumped to `e3`.
   - **Queries** (`find_callers/callees/impact`) now run on resolved edges and
     take `min_conf`; CLI `--strict` / `--min-confidence`, mirrored as MCP
     `strict`/`min_confidence`. Formatters print the level (`[medium]`).
   - 15 new tests (33 total for edges/resolution); **84 pass** (8 ts-gated).
7. **Go + Java import resolution (Phase 3) — DONE** (2026-06-30, branch
   `feat/go-java-resolution`): extends resolution to the *receiver-qualified*
   call models. Go `util.Func()` and Java `Util.method()` aren't free calls — the
   receiver IS the import — so a new receiver-import tier (1c) resolves them:
   - Bindings: Go `import "p/q/util"`/grouped blocks/aliases → `util`→path; Java
     `import a.b.C;` (+ `import static`) → `C`→`a.b.C`; Python `import x as y`.
   - Resolvers (no go.mod / source-root parse — suffix matching): Go import path →
     repo package dir (longest suffix wins, beats a same-named decoy); Java FQCN →
     `a/b/C.java` tolerating a `src/main/java/…` prefix.
   - The import tier runs before the class-registry tier so the exact package
     disambiguates same-named classes. Go resolves on any backend (top-level funcs
     are regex-visible); Java needs ctags/tree-sitter (regex misses methods).
   - 8 new tests; **92 pass** (9 ts-gated).
8. **Receiver-variable type tracking (Phase 4) — DONE** (2026-06-30, branch
   `feat/var-type-resolution`): resolves instance-method calls `x.run()` when
   `x`'s type is known from a constructor-style assignment in the same function.
   - `extract_var_types` (regex, all backends) records `(enclosing, var, type,
     line)` from `x = Foo()` (Py), `new Foo()` (JS/TS/Java), `Foo{}` / `NewFoo()`
     / `var x Foo` (Go); only capitalized RHS types (class-name convention).
     Persisted as `vtypes` under `--edges`; `build_key` edge tag → `e4`.
   - resolve_edges Tier 1e: a receiver var with a tracked type → `_lookup_method`
     on that class hierarchy; high when the var's type is unambiguous in scope,
     medium when several types were assigned. Coarse (per-function) scoping; the
     method lookup needs qualified methods so it's ctags/tree-sitter-effective.
   - 8 new tests; **100 pass** (10 ts-gated).
   The resolution ladder is now: self/super → import (recv) → known class →
   var-type → free-call local/import → name fallback. Remaining (deliberately
   out): full type inference (factory returns, reassignment, generics/overloads),
   framework dynamic-dispatch synthesis, and embeddings for semantic search.

9. **`explore` / `node` — source-returning queries (2026-07-10)**, closing the
   one agent-ergonomics gap vs. daemon-backed indexers like **codegraph**: every
   other query returned a `path:line` that forced a follow-up Read, whereas
   `codegraph explore`/`node` hand back the *source*. Now repograph does too, on
   the existing index+resolved-edges, staying stdlib-only (no daemon/DB).
   - **`node(repo, name)`** — the top N (default 3) definition sites' verbatim,
     line-numbered source + caller (`<-`) / callee (`->`) trail, confidence-tagged.
   - **`explore(repo, query, max_files)`** — resolves a query (identifier tokens
     via `find_symbol` + lexical `search_symbols` ranking) to ≤`max_files`
     symbols, prints each one's source, then the resolved call edges *between the
     picked set* ("call paths between these symbols").
   - Source slices use `Symbol.end` when the backend knew it (tree-sitter/ctags),
     else fall back to next-symbol-start − 1; capped to `SOURCE_MAX_LINES` (80)
     with an elision note. Helpers: `_symbol_span`, `_render_source`,
     `_find_symbol_object`, `_files_by_rel`.
   - CLI: `--node NAME`, `--explore QUERY`, `--max-files N` (all force edges on,
     honor `--strict`/`--min-confidence` — strict cleanly drops the regex
     backend's docstring-prose call false-positives from the trail).
   - MCP: `node` + `explore` tools (now **ten** total). 9 new tests; **109 pass**
     (10 ts-gated). Docs updated in README + skill/SKILL.md.
   Deliberately NOT adopted from codegraph: the background daemon + SQLite index
   (repograph's niche is the committed, zero-dep, human-readable map — a live
   daemon would abandon that). Positioning stands: lightweight/committable vs.
   codegraph's live/heavier indexer; the two don't integrate.
   Still deliberately out (would break the zero-dep default): **embeddings** for
   *true* semantic search. CJS `module.exports` names in the regex path also
   still missing.

## Pointers

- Tool: [repograph.py](repograph.py) — `LANGS` table + `JS_TS_*` lists for
  language rules; `build_repo` for the incremental logic; `render_*` for formats.
- Tests: [tests/test_repograph.py](tests/test_repograph.py) — fixture repo across
  languages; asserts correct symbol line numbers, noise-dir skipping, filters,
  json round-trip, incremental reuse/drop, TS typed arrows + enum, Elixir. Plus
  `CtagsTest`: version gate, NDJSON parse/dedupe, scope qualification, level
  filtering, grouped index rendering, symbol cap, empty→regex fallback (all via
  stubs — no ctags binary needed).
- Original build plan (pre-move, now largely superseded by this file):
  `~/.claude/plans/before-we-proceed-the-iridescent-crab.md`.
