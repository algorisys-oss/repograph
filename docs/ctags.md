# ctags & repograph — a practical reference

How repograph uses [universal-ctags](https://github.com/universal-ctags/ctags),
how to use ctags directly, and what it's good (and not good) for.

## What ctags is

Universal Ctags scans source and emits an index of where every symbol — function,
class, method, struct, … — is **defined**: `name`, `kind`, `line`, and `scope`.
It's a fast, language-aware lexer covering ~150 languages, CPU-only, no model.

> **Universal, not Exuberant.** Only the maintained *Universal Ctags* fork has
> JSON output (`--output-format=json`), which repograph requires. The old
> *Exuberant Ctags* won't work — repograph detects this and falls back to regex.

```bash
# install
sudo apt-get install universal-ctags     # Debian/Ubuntu
brew install universal-ctags             # macOS

# confirm it's the Universal fork
ctags --version          # must print "Universal Ctags"
```

## How repograph uses it

It's **automatic**: if a `ctags` binary is on `PATH`, repograph uses it for the
symbol layer; otherwise it silently falls back to its built-in regex extractor.
Imports and leading-doc are always regex (ctags doesn't model them).

```bash
repograph.py .                          # ctags auto-used when present
repograph.py . --no-ctags               # force the regex backend
repograph.py . --ctags-path /opt/bin/ctags   # use a specific binary
```

Under the hood repograph runs one batch invocation (per build, over changed
files only, paths fed on stdin):

```bash
ctags --quiet --output-format=json --fields=+nKzS -f - -L -
```

which yields newline-delimited JSON like:

```json
{"_type":"tag","name":"connect","kind":"method","line":157,
 "scope":"AdkWebSocketClient","scopeKind":"class","path":"src/api/index.js"}
```

repograph maps `kind` → its own vocabulary, uses `scope` to qualify methods as
`Owner.method`, and renders them grouped in the index:

```
src/api/index.js | javascript | 515 | … AdkWebSocketClient:130{connect:157 disconnect:224 …}
```

**Why it matters vs. the regex fallback:** ctags captures **class/object methods**
(which regex misses entirely for JS/TS/Java) and **qualifies** them with their
owning type. See [the README's "Symbol extraction" section](../README.md#symbol-extraction-ctags-fast-path--regex-fallback).

### One kind nuance repograph handles for you

ctags tags methods differently per language: JS/TS/Java/Ruby use kind `method`,
but **Python tags class methods as `member`** (with `scopeKind: class`), the same
kind it uses for C struct *fields*. repograph promotes a `member` to a method
(qualified, kept at `--symbols=defs`) only when its `scopeKind` is an OO container
(class/interface/trait/module/object) — so Python methods are kept while C/struct
data fields stay fields. You don't need to think about it; it's just why Python
methods show up correctly.

## Using ctags directly (handy commands)

```bash
# Classic editor "tags" file (vim: Ctrl-] to jump to definition)
ctags -R .                              # writes ./tags for the whole tree
ctags -R --languages=Python,Go .

# JSON for tooling (what repograph consumes)
ctags --output-format=json --fields=+nKzS -f - src/api/index.js

# Quick human-readable symbol list for one file
ctags -x src/foo.py                     # name  kind  line  source-line

# Discover capabilities
ctags --list-languages                  # ~150 languages
ctags --list-kinds=Python               # kinds per language (c=class f=function m=member…)
ctags --list-fields                     # fields: +n line, +K kind, +S/+s scope, +r reference…

# Reference (usage) tags — partial, per-language, off by default
ctags --extras=+r --output-format=json -f - src/foo.c
```

Useful `--fields` flags: `+n` line number, `+K` kind **name** (not the one-letter
code — names are stable across languages), `+S`/`+s` scope, `+z` force the `kind`
key into JSON, `+r` reference tags.

## Use cases

- **Editor jump-to-definition** — the original use: `ctags -R .` then `Ctrl-]` in
  vim / helpers in emacs. Offline, no language server needed.
- **Symbol index for tools/agents** — exactly what repograph does: a compact,
  precise definition map an LLM can route on.
- **Code navigation / outlines** — feed the JSON to scripts for "where is X
  defined", file outlines, or cross-reference lists.
- **CI / code-quality checks** — parse the tags to assert e.g. every public symbol
  has a docstring, or flag naming-convention violations.
- **Coverage where you have no LSP** — handles niche/legacy languages cheaply.

## Limits (know the boundary)

ctags indexes **definitions**, not a resolved call graph or types. Its
reference/usage support (`--extras=+r`) is partial and language-dependent — it
can't reliably answer "who *calls* this exact method". That's why repograph uses
ctags for precise definitions, lexical `find_refs` (git grep) for *approximate*
usages, and leaves resolved call graphs / semantic search to compose-with tools
(an LSP, or an embedding index) rather than rebuilding them.
