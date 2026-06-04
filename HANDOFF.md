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

## Status: working, 28 tests passing

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
- **Language-agnostic + shallow (regex)** — chosen over tree-sitter/ctags to keep
  zero-dependency and work on any repo. Accepted cost: approximate symbols, and
  imports/relationships are the weakest layer. ctags/tree-sitter is the
  documented accuracy upgrade path if/when needed.
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
one batch ctags call). ctags is **not installed on this machine** → the live
path here is the regex fallback; the ctags path is covered by stubbed tests and
was verified end-to-end against a fake universal-ctags binary.

**Token guardrails** (the reduced-tokens goal): `--symbols {none,defs,full}`,
default `defs` (top-level defs + qualified methods, drops members/fields); the
index groups methods under their owner as `Owner{m:line …}` so the class prefix
is written once; per-file symbol cap `MAX_SYMBOLS_PER_FILE` (80) with `+N more`.
Net: the methods gap is fixed while the index stays ~flat in size.

### Honest limits (still)
- Without universal-ctags, the **regex fallback** misses JS/TS/Java methods and
  doesn't qualify names (install ctags to fix). CJS `module.exports = {…}` names
  still aren't pulled out by the regex path.
- `.vue/.svelte/.heex/.eex` templates get a file node but no symbols.
- Imports are always regex (weakest layer). No type resolution, no call graph,
  no cross-file reference edges — tree-sitter is the next step there.

## Open threads / next steps

1. **examples/ cleanup (undecided).** `examples/ziglang-zig.repomap.md` (1.6 MB)
   + `ziglang-zig.graph.json` (1.5 MB) are ~3 MB of regenerable, zig-specific
   output that rode along in the move. Recommendation: delete both and let the
   README's generate-command stand (or replace with a tiny self-example). README
   currently references them, so trim that section if removed.
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
4. **Accuracy upgrades — ctags DONE** (2026-06-04, see "Symbol backend" above):
   optional universal-ctags backend gives precise symbols incl. qualified
   class/methods, with regex fallback. Still open if needed: git-status fast
   path for change detection on huge repos; a **tree-sitter** backend for type
   resolution / call graph / cross-file reference edges (the things ctags also
   can't give us); CJS `module.exports` names in the regex path.

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
