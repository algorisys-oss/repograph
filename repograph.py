#!/usr/bin/env python3
"""repograph — build a language-agnostic knowledge graph of a local repo.

Point it at a directory and it emits a Markdown "repo map": an overview, a
directory tree, and a per-file breakdown of symbols (functions/types/classes),
leading docs, and imports. Every file and symbol links back to the original
source as `path#Lnn`, so the map doubles as an index into the code.

Extraction is *shallow and heuristic by default* — regex per language, no real
parsing — which is what makes it work on any repo with zero dependencies. It will
miss and mis-tag some things, especially imports; sections say so. Two optional
backends raise precision when present: universal-ctags (auto-detected) and
tree-sitter (opt-in via --tree-sitter), the latter also yielding an accurate call
graph.

With --edges (or any of the --callers/--callees/--impact/--affected queries) it
also extracts relationship edges (calls / extends / implements) and can answer
call-graph questions — approximate on the regex backend, precise on tree-sitter.

Usage:
    python repograph.py <repo-path> [-o REPOMAP.md]

Standard library only (the optional tree-sitter backend needs a wheel). Python 3.8+.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Language table: extension -> language config.
#
# Each language has:
#   symbols: list of (compiled regex, kind). The regex must have one capture
#            group naming the symbol. Matched line-by-line.
#   imports: list of compiled regexes, each with one capture group = the imported
#            module/path. Matched line-by-line.
#
# Adding a language is a few lines here; nothing else needs to change.
# --------------------------------------------------------------------------- #


def _c(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern)


LANGS = {
    "python": {
        "exts": [".py", ".pyi"],
        "symbols": [
            (_c(r"^\s*def\s+(\w+)"), "function"),
            (_c(r"^\s*class\s+(\w+)"), "class"),
        ],
        "imports": [
            _c(r"^\s*import\s+([\w.]+)"),
            _c(r"^\s*from\s+([\w.]+)\s+import"),
        ],
    },
    # JavaScript and TypeScript share the same extraction rules (below); they're
    # separate languages only so the breakdown labels them distinctly.
    "javascript": {
        "exts": [".js", ".jsx", ".mjs", ".cjs"],
        "symbols": "JS_TS",   # resolved to JS_TS_SYMBOLS after the table
        "imports": "JS_TS",
    },
    "typescript": {
        "exts": [".ts", ".tsx", ".mts", ".cts"],
        "symbols": "JS_TS",
        "imports": "JS_TS",
    },
    "zig": {
        "exts": [".zig"],
        "symbols": [
            (_c(r"^\s*(?:pub\s+)?fn\s+(\w+)"), "function"),
            (_c(r"^\s*(?:pub\s+)?const\s+(\w+)\s*=\s*(?:extern\s+|packed\s+)?(?:struct|enum|union|opaque)"), "type"),
        ],
        "imports": [
            _c(r'@import\(\s*"([^"]+)"\s*\)'),
        ],
    },
    "c": {
        "exts": [".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".hh"],
        "symbols": [
            (_c(r"^[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;]*\)\s*\{"), "function"),
            (_c(r"^\s*(?:typedef\s+)?struct\s+(\w+)"), "struct"),
            (_c(r"^\s*#define\s+(\w+)"), "macro"),
        ],
        "imports": [
            _c(r'^\s*#include\s*[<"]([^>"]+)[>"]'),
        ],
    },
    "go": {
        "exts": [".go"],
        "symbols": [
            (_c(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"), "function"),
            (_c(r"^\s*type\s+(\w+)"), "type"),
        ],
        "imports": [
            _c(r'^\s*import\s+"([^"]+)"'),
            _c(r'^\s*"([^"]+)"\s*$'),  # block-import line; noisy but cheap
        ],
    },
    "rust": {
        "exts": [".rs"],
        "symbols": [
            (_c(r"^\s*(?:pub\s+)?fn\s+(\w+)"), "function"),
            (_c(r"^\s*(?:pub\s+)?struct\s+(\w+)"), "struct"),
            (_c(r"^\s*(?:pub\s+)?enum\s+(\w+)"), "enum"),
            (_c(r"^\s*(?:pub\s+)?trait\s+(\w+)"), "trait"),
        ],
        "imports": [
            _c(r"^\s*use\s+([\w:]+)"),
        ],
    },
    "ruby": {
        "exts": [".rb"],
        "symbols": [
            (_c(r"^\s*def\s+(\w+)"), "function"),
            (_c(r"^\s*class\s+(\w+)"), "class"),
            (_c(r"^\s*module\s+(\w+)"), "module"),
        ],
        "imports": [
            _c(r"^\s*require(?:_relative)?\s+['\"]([^'\"]+)['\"]"),
        ],
    },
    "java": {
        "exts": [".java"],
        "symbols": [
            (_c(r"^\s*(?:public|private|protected)?\s*(?:abstract\s+|final\s+)?class\s+(\w+)"), "class"),
            (_c(r"^\s*(?:public|private|protected)?\s*interface\s+(\w+)"), "interface"),
            (_c(r"^\s*(?:public|private|protected)?\s*enum\s+(\w+)"), "enum"),
        ],
        "imports": [
            _c(r"^\s*import\s+([\w.]+);"),
        ],
    },
    "shell": {
        "exts": [".sh", ".bash"],
        "symbols": [
            (_c(r"^\s*function\s+(\w+)"), "function"),
            (_c(r"^\s*(\w+)\s*\(\)\s*\{"), "function"),
        ],
        "imports": [
            _c(r"^\s*(?:source|\.)\s+(\S+)"),
        ],
    },
    "elixir": {
        "exts": [".ex", ".exs"],
        "symbols": [
            (_c(r"^\s*defmodule\s+([\w.]+)"), "module"),
            (_c(r"^\s*defprotocol\s+([\w.]+)"), "protocol"),
            (_c(r"^\s*defmacro\s+(\w+)"), "macro"),
            (_c(r"^\s*defp\s+(\w+)"), "function"),
            (_c(r"^\s*def\s+(\w+)"), "function"),
        ],
        "imports": [
            _c(r"^\s*(?:import|alias|use|require)\s+([\w.]+)"),
        ],
    },
}

# Shared JavaScript/TypeScript rules. The arrow-function pattern allows an
# optional `: Type` annotation and return type (so typed React/Solid components
# like `const X: Component<P> = (props) => ...` are caught), generics, and
# single-arg arrows. enum is TS-only but harmless to look for in JS.
_TYPE = r"[\w.<>,\[\]\|'\" ]"
JS_TS_SYMBOLS = [
    (_c(r"\bfunction\s+(\w+)"), "function"),
    (_c(r"\bclass\s+(\w+)"), "class"),
    (_c(
        r"\b(?:export\s+)?(?:default\s+)?const\s+(\w+)\s*"
        r"(?::\s*" + _TYPE + r"+?)?\s*=\s*(?:async\s+)?(?:<[^>]*>\s*)?"
        r"(?:\([^)]*\)|\w+)\s*(?::\s*" + _TYPE + r"+?)?\s*=>"
    ), "function"),
    (_c(r"\b(?:export\s+)?(?:interface|type)\s+(\w+)"), "type"),
    (_c(r"\b(?:export\s+)?(?:const\s+)?enum\s+(\w+)"), "enum"),
]
JS_TS_IMPORTS = [
    _c(r"\bimport\s+.*\bfrom\s+['\"]([^'\"]+)['\"]"),
    _c(r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)"),
]
for _cfg in LANGS.values():
    if _cfg["symbols"] == "JS_TS":
        _cfg["symbols"] = JS_TS_SYMBOLS
    if _cfg["imports"] == "JS_TS":
        _cfg["imports"] = JS_TS_IMPORTS

# Reverse index: extension -> language name.
EXT_TO_LANG = {ext: name for name, cfg in LANGS.items() for ext in cfg["exts"]}

# --------------------------------------------------------------------------- #
# Inheritance ("extends"/"implements") patterns — heuristic, per language.
#
# Each entry yields (child, parents_blob, kind_for_blob) on a class/type header;
# parents_blob is split on commas into individual base names. Languages model
# inheritance differently (single base vs. interface list vs. trait impl), so the
# regexes are tailored; absent languages simply contribute no inheritance edges.
# --------------------------------------------------------------------------- #
INHERIT = {
    # class X(Base1, Base2): — every base treated as "extends".
    "python": [(_c(r"^\s*class\s+(\w+)\s*\(([^)]*)\)"), "extends")],
    # class X extends Y { / class X implements I, J { — captured separately.
    "java": [
        (_c(r"\b(?:class|interface)\s+(\w+)[^{]*?\bextends\s+([\w.<>, ]+?)(?:\s+implements|\s*\{|$)"), "extends"),
        (_c(r"\bclass\s+(\w+)[^{]*?\bimplements\s+([\w.<>, ]+?)(?:\s*\{|$)"), "implements"),
    ],
    # class X extends Y implements I (TS) / class X extends Y (JS).
    "javascript": [(_c(r"\bclass\s+(\w+)\s+extends\s+([\w.]+)"), "extends")],
    "typescript": [
        (_c(r"\bclass\s+(\w+)\s+extends\s+([\w.]+)"), "extends"),
        (_c(r"\bclass\s+(\w+)[^{]*?\bimplements\s+([\w.<>, ]+?)(?:\s*\{|$)"), "implements"),
        (_c(r"\binterface\s+(\w+)\s+extends\s+([\w.<>, ]+?)(?:\s*\{|$)"), "extends"),
    ],
    # class X < Y — Ruby single superclass.
    "ruby": [(_c(r"^\s*class\s+(\w+)\s*<\s*([\w:]+)"), "extends")],
    # class X : public Base1, Base2 — C++ (strip access specifiers when splitting).
    "c": [(_c(r"\b(?:class|struct)\s+(\w+)\s*:\s*([\w:,<> ]+?)\s*\{"), "extends")],
    # impl Trait for Type — Rust: Type implements Trait.
    "rust": [(_c(r"^\s*impl\s+([\w:<>, ]+?)\s+for\s+(\w+)"), "_rust_impl")],
}

# Identifiers that look like calls (`name(`) but are language keywords, not
# functions — filtered out so the call graph isn't polluted by control flow.
_CALL_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "with", "match", "case",
    "elif", "except", "function", "def", "fn", "func", "class", "struct", "enum",
    "interface", "trait", "and", "or", "not", "in", "is", "await", "yield",
    "print", "len", "range", "super", "self", "new", "typeof", "sizeof",
}

# A free call site: an identifier immediately followed by "(", not preceded by a
# "." (those are method calls, captured separately with their receiver below).
_CALL_RE = _c(r"(?:^|[^\w.])(\w+)\s*\(")
# A method call `recv.name(`: captures the receiver and the called name so the
# resolver can bind it (e.g. `self.run()` → the enclosing class's `run`).
_METHOD_CALL_RE = _c(r"(\w+)\s*\.\s*(\w+)\s*\(")

# Comment markers used to grab a file's leading doc block.
LINE_COMMENT = {
    "python": "#", "ruby": "#", "shell": "#", "elixir": "#",
    "javascript": "//", "typescript": "//", "c": "//", "go": "//",
    "rust": "//", "java": "//", "zig": "//",
}

# Directories we never descend into when not using git.
NOISE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", ".zig-cache", "zig-cache", "vendor", ".idea",
    ".vscode", ".mypy_cache", ".pytest_cache", "__snapshots__",
    ".repograph",  # repograph's own generated index/cache — never index it
}

MAX_FILE_BYTES = 2_000_000  # skip files larger than this

# Render at most this many symbols per file in the index (overflow → "+N more").
MAX_SYMBOLS_PER_FILE = 80

# --------------------------------------------------------------------------- #
# Optional universal-ctags backend (precise symbols, used when available).
#
# ctags replaces ONLY the symbol layer; imports + leading docs stay regex. It is
# an optional external binary — when absent, everything falls back to the regex
# extraction below, preserving the zero-dependency behavior exactly.
# --------------------------------------------------------------------------- #

# Map ctags kind *names* (not single-letter codes — those collide across
# languages) to this tool's kind vocabulary. Unmapped kinds pass through verbatim.
CTAGS_KIND_MAP = {
    "function": "function", "func": "function", "subroutine": "function",
    "method": "method", "member function": "method",
    "class": "class", "struct": "struct",
    "enum": "enum", "enumerator": "enum",
    "interface": "interface", "trait": "trait",
    "module": "module", "namespace": "module", "package": "module",
    "protocol": "protocol", "macro": "macro",
    "typedef": "type", "type": "type", "alias": "type", "typealias": "type",
    "union": "struct",
    "member": "member", "field": "field", "property": "field",
    "variable": "field", "constant": "field",
}

# Kinds that are method-like — qualified with their owning scope (Owner.name).
_METHOD_KINDS = {"method"}

# scopeKinds that mark an OO container. Some languages (Python, …) tag class
# methods as kind "member" rather than "method"; we promote a member to a method
# only when its owner is one of these — NOT struct/union, whose members are data
# fields (e.g. C struct fields), which must stay fields (dropped at "defs").
_CLASS_SCOPE_KINDS = {
    "class", "interface", "trait", "module", "object", "mixin", "role",
}

# Kinds kept at the default --symbols=defs level. High-cardinality members and
# fields are dropped here (they explode the index) and only kept at "full".
_DEFAULT_KINDS = {
    "function", "method", "class", "struct", "enum", "interface", "trait",
    "module", "protocol", "macro", "type",
}

# Set by --ctags PATH; the detector honors it. None → look for "ctags" on PATH.
_CTAGS_BIN = None
# Memoized result of ctags_available(): None = not yet probed.
_CTAGS_OK = None


# --------------------------------------------------------------------------- #
# Data model — the graph.
# --------------------------------------------------------------------------- #


@dataclass
class Symbol:
    name: str
    kind: str
    line: int  # 1-based, definition line
    end: int = 0  # 1-based last line of the symbol's body; 0 = unknown


@dataclass
class Edge:
    """A directed relationship between a symbol and a name it references.

    `src` is the *unqualified* name of the enclosing symbol in this file (or ""
    when the reference sits at file scope). `dst` is the referenced name, left
    *unresolved* — query time matches it against the global definition table.
    Keeping edges name-based (not pre-resolved) means a file's edges depend only
    on that file, so the incremental cache stays correct per-file.
    """
    src: str    # enclosing symbol name, or "" for file scope
    dst: str    # referenced name (unresolved)
    kind: str   # "calls" | "extends" | "implements"
    line: int   # 1-based site of the reference
    recv: str = ""  # call receiver token: self/this/super, a var name, or "" (calls)


@dataclass
class FileNode:
    rel_path: str          # POSIX-style, relative to repo root
    language: str          # language name or "" if unknown
    line_count: int
    content_hash: str = "" # sha1 of file bytes — drives incremental update
    gitsha: str = ""       # git blob sha (when tracked) — git-status fast path
    build_key: str = ""    # "<level>:<ctags?>:<backend>:<edges?>" — invalidates reuse
    doc: str = ""          # leading comment/docstring, trimmed
    symbols: list = field(default_factory=list)   # list[Symbol]
    imports: list = field(default_factory=list)   # list[str]
    edges: list = field(default_factory=list)     # list[Edge]
    import_bindings: list = field(default_factory=list)  # list[(local_name, module)]
    var_types: list = field(default_factory=list)  # list[(enclosing, var, type, line)]


@dataclass
class Repo:
    root: Path
    name: str
    files: list = field(default_factory=list)      # list[FileNode]
    readme_summary: str = ""
    # Incremental-update stats for the current run (CLI feedback).
    reused: int = 0
    analyzed: int = 0
    dropped: int = 0


# --------------------------------------------------------------------------- #
# Walking the repo.
# --------------------------------------------------------------------------- #


def list_files(root: Path) -> "list[Path]":
    """Return repo files as absolute paths.

    Prefer git when root is a git repo: `git ls-files --cached --others
    --exclude-standard` lists tracked *and* untracked-not-ignored files, so the
    map reflects new working-tree files while still honoring .gitignore.
    Otherwise os.walk while skipping noise directories. repograph's own
    `.repograph/` artifacts are always excluded (see NOISE_DIRS).
    """
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z",
                 "--cached", "--others", "--exclude-standard"],
                capture_output=True, check=True,
            )
            rels = out.stdout.decode("utf-8", "replace").split("\0")
            return [root / r for r in rels
                    if r and ".repograph" not in Path(r).parts]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass  # git missing or not a real repo — fall back to walk

    found: "list[Path]" = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in NOISE_DIRS]
        for fn in filenames:
            found.append(Path(dirpath) / fn)
    return found


def git_blob_shas(root: Path) -> "dict[str, str] | None":
    """{rel_path: git blob sha} for tracked files, or None if not a git repo.

    `git ls-files -s` reports each file's blob object id *as recorded in the
    index* — a content fingerprint we can read without opening the file. This
    powers the change-detection fast path: an unchanged tracked file keeps its
    blob sha, so we can reuse its cached node without reading + hashing it.
    """
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-s", "-z"],
            capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    shas: "dict[str, str]" = {}
    for entry in out.stdout.decode("utf-8", "replace").split("\0"):
        if not entry:
            continue
        # format: "<mode> <objectid> <stage>\t<path>"
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if path and len(parts) >= 2:
            shas[path] = parts[1]
    return shas


def git_dirty_files(root: Path) -> "set[str]":
    """Tracked paths whose working tree differs from the index (porcelain).

    The index blob sha (from git_blob_shas) is stale for files with unstaged
    edits; those show up here, so we fall back to reading + hashing them.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-z",
             "--untracked-files=no"],
            capture_output=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
    dirty: "set[str]" = set()
    # -z records are NUL-separated; "XY <path>" (rename adds a second NUL field).
    fields = out.stdout.decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        if not rec:
            i += 1
            continue
        status, path = rec[:2], rec[3:]
        if path:
            dirty.add(path)
        if "R" in status:  # rename: the next field is the old path
            i += 1
        i += 1
    return dirty


def is_binary(path: Path) -> bool:
    """Cheap binary sniff: a NUL byte in the first 4 KiB means binary."""
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(4096)
    except OSError:
        return True


def read_lines(path: Path) -> "list[str]":
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read().splitlines()


def content_hash(data: bytes) -> str:
    """sha1 of raw bytes — used to tell whether a file changed since last run."""
    return hashlib.sha1(data).hexdigest()


# --------------------------------------------------------------------------- #
# Per-file extraction.
# --------------------------------------------------------------------------- #


def extract_symbols(lines, cfg) -> "list[Symbol]":
    symbols: "list[Symbol]" = []
    for i, line in enumerate(lines, start=1):
        for pattern, kind in cfg["symbols"]:
            m = pattern.search(line)
            if m:
                symbols.append(Symbol(name=m.group(1), kind=kind, line=i))
                break  # one symbol kind per line is plenty
    return symbols


def extract_imports(lines, cfg) -> "list[str]":
    seen = set()
    order: "list[str]" = []
    for line in lines:
        for pattern in cfg["imports"]:
            m = pattern.search(line)
            if m:
                mod = m.group(1)
                if mod not in seen:
                    seen.add(mod)
                    order.append(mod)
                break
    return order


# Python: `from <module> import a, b as c` (single line, optionally parenthesized).
_PY_FROM = _c(r"^\s*from\s+(\.*[\w.]*)\s+import\s+(.+)$")
# Python: `import a.b` / `import a.b as c` (binds a callable receiver name).
_PY_IMPORT = _c(r"^\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?\s*$")
# JS/TS: `import <clause> from '<module>'` and bare `import '<module>'`.
_JS_IMPORT = _c(r"^\s*import\s+(.+?)\s+from\s+['\"]([^'\"]+)['\"]")
_JS_NAME = _c(r"(\w+)(?:\s+as\s+(\w+))?")
# Go: `import "path"` or `alias "path"` (single line or inside an import block).
_GO_IMPORT_ONE = _c(r'^\s*import\s+(?:(\w+|\.|_)\s+)?"([^"]+)"')
_GO_BLOCK_LINE = _c(r'^\s*(?:(\w+|\.|_)\s+)?"([^"]+)"')
# Java: `import a.b.C;` / `import static a.b.C.m;`.
_JAVA_IMPORT = _c(r"^\s*import\s+(static\s+)?([\w.]+)\s*;")


def extract_import_bindings(lines, language: str) -> "list[tuple]":
    """Map locally-bound names to the module they came from: [(local_name, module)].

    Drives import-resolution of call edges — both *free* calls (`helper()` from a
    JS/Python `import`) and *qualified* ones (`util.Func()` in Go, `C.method()` in
    Java, where the receiver IS the imported name). Heuristic and mostly
    single-line. Other languages → [] (no bindings).
    """
    out: "list[tuple]" = []
    if language == "python":
        for line in lines:
            m = _PY_FROM.search(line)
            if m:
                module = m.group(1)
                names = m.group(2).strip().strip("()").rstrip("\\").strip()
                if names != "*":
                    for chunk in names.split(","):
                        chunk = chunk.strip()
                        if chunk:
                            local = chunk.split()[-1]  # "a" | "a as b" -> last
                            if re.fullmatch(r"\w+", local):
                                out.append((local, module))
                continue
            m = _PY_IMPORT.search(line)
            if m:
                module, alias = m.group(1), m.group(2)
                # bind a simple receiver name: the alias, or a single-segment module
                if alias:
                    out.append((alias, module))
                elif "." not in module:
                    out.append((module, module))
    elif language in ("javascript", "typescript"):
        for line in lines:
            m = _JS_IMPORT.search(line)
            if not m:
                continue
            clause, module = m.group(1).strip(), m.group(2)
            head = clause.split("{")[0].split(",")[0].strip()  # default import
            if re.fullmatch(r"\w+", head):
                out.append((head, module))
            nm = re.search(r"\{([^}]*)\}", clause)
            if nm:
                for chunk in nm.group(1).split(","):
                    g = _JS_NAME.search(chunk.strip())
                    if g:
                        out.append((g.group(2) or g.group(1), module))
            ns = re.search(r"\*\s+as\s+(\w+)", clause)
            if ns:
                out.append((ns.group(1), module))
    elif language == "go":
        in_block = False
        for line in lines:
            stripped = line.strip()
            if not in_block:
                if re.match(r"^\s*import\s*\($", line):
                    in_block = True
                    continue
                m = _GO_IMPORT_ONE.search(line)
                if m:
                    _go_bind(out, m.group(1), m.group(2))
            else:
                if stripped.startswith(")"):
                    in_block = False
                    continue
                m = _GO_BLOCK_LINE.search(line)
                if m:
                    _go_bind(out, m.group(1), m.group(2))
    elif language == "java":
        for line in lines:
            m = _JAVA_IMPORT.search(line)
            if not m:
                continue
            is_static, path = bool(m.group(1)), m.group(2)
            if path.endswith(".*"):
                continue  # wildcard import binds no single name
            parts = path.split(".")
            if is_static:
                # `import static a.b.C.m` -> name m, defined in class file a/b/C
                out.append((parts[-1], ".".join(parts[:-1])))
            else:
                out.append((parts[-1], path))  # class C bound to its full path
    return out


def _go_bind(out, alias, path):
    """Record a Go import binding: local package name -> import path."""
    if alias in (".", "_"):
        return  # dot/blank imports bind no usable receiver
    local = alias or path.rsplit("/", 1)[-1]  # default: last path element
    if re.fullmatch(r"\w+", local or ""):
        out.append((local, path))


# Local-variable type assignments, per language: (regex, var_group, type_groups).
# `type_groups` is tried in order; the first non-None capture is the type (so an
# explicit annotation/declaration wins over the constructed value). Only
# capitalized RHS types are kept — the near-universal class-name convention —
# which is what makes `x = Foo()` / `new Foo()` / `Foo{}` distinguishable from a
# plain function call. Receiver-type tracking for `x.method()` (Phase 4).
_VARTYPE = {
    "python": [
        (_c(r"^\s*(\w+)\s*:\s*([A-Z]\w*)\s*="), 1, (2,)),     # x: Foo = ...
        (_c(r"^\s*(\w+)\s*=\s*([A-Z]\w*)\s*\("), 1, (2,)),    # x = Foo(...)
    ],
    "javascript": [
        (_c(r"\b(?:const|let|var)\s+(\w+)\s*:\s*([A-Z]\w*)"), 1, (2,)),
        (_c(r"\b(?:const|let|var)\s+(\w+)\s*=\s*new\s+([A-Z]\w*)"), 1, (2,)),
    ],
    "java": [
        (_c(r"\b([A-Z]\w*)\s+(\w+)\s*=\s*new\s+[A-Z]\w*"), 2, (1,)),  # Foo x = new Foo()
        (_c(r"\bvar\s+(\w+)\s*=\s*new\s+([A-Z]\w*)"), 1, (2,)),       # var x = new Foo()
    ],
    "go": [
        (_c(r"\b(\w+)\s*:=\s*&?([A-Z]\w*)\s*\{"), 1, (2,)),   # x := Foo{} / &Foo{}
        (_c(r"\b(\w+)\s*:=\s*New([A-Z]\w*)\s*\("), 1, (2,)),  # x := NewFoo(...)
        (_c(r"\bvar\s+(\w+)\s+([A-Z]\w*)\b"), 1, (2,)),       # var x Foo
    ],
}
_VARTYPE["typescript"] = _VARTYPE["javascript"]


def extract_var_types(lines, language: str, symbols) -> "list[tuple]":
    """Track local `var -> Type` from constructor-style assignments, attributed to
    the enclosing definition: [(enclosing, var, type, line)].

    Lets the resolver bind `x.method()` when `x = Foo()` is in scope. Heuristic:
    relies on the capitalized-class-name convention and coarse (per-function)
    scoping. Other languages → [].
    """
    pats = _VARTYPE.get(language)
    if not pats:
        return []
    sorted_syms = sorted(symbols, key=lambda s: s.line)
    n = len(sorted_syms)
    si = 0
    cur = ""  # enclosing symbol; "" = file scope
    out: "list[tuple]" = []
    seen: "set[tuple]" = set()
    for i, line in enumerate(lines, start=1):
        while si < n and sorted_syms[si].line <= i:
            cur = sorted_syms[si].name
            si += 1
        for pat, vg, tgs in pats:
            m = pat.search(line)
            if not m:
                continue
            var = m.group(vg)
            typ = next((m.group(g) for g in tgs if m.group(g)), None)
            if (var and typ and re.fullmatch(r"\w+", var)
                    and re.fullmatch(r"[A-Z]\w*", typ)):
                key = (cur, var, typ)
                if key not in seen:
                    seen.add(key)
                    out.append((cur, var, typ, i))
            break
    return out


def assign_spans(symbols, total_lines: int) -> None:
    """Fill in end lines for symbols lacking them (end == 0), in place.

    Flat heuristic: a symbol runs until the next symbol's definition line (last
    one to end of file). Enough to attribute a reference to its nearest enclosing
    definition; tree-sitter and ctags supply exact ends when available.
    """
    ordered = sorted(range(len(symbols)), key=lambda i: symbols[i].line)
    for pos, i in enumerate(ordered):
        if symbols[i].end:
            continue
        if pos + 1 < len(ordered):
            nxt = symbols[ordered[pos + 1]].line - 1
        else:
            nxt = total_lines
        symbols[i].end = max(symbols[i].line, nxt)


def _base_name(raw: str) -> str:
    """Reduce a base-type expression to a bare identifier.

    Strips generics, access specifiers, and namespace/package qualifiers, so
    `public std::vector<T>` / `Foo.Bar<Baz>` collapse to `vector` / `Bar`.
    """
    raw = re.sub(r"<.*", "", raw or "")
    raw = re.sub(r"\b(?:public|private|protected|virtual|final|abstract)\b", "", raw)
    last = re.split(r"::|\.", raw.strip())[-1].strip()
    return last if re.fullmatch(r"\w+", last or "") else ""


def extract_inherits(lines, language: str) -> "list[Edge]":
    """Heuristic extends/implements edges from class/type headers.

    Tailored per language (see INHERIT); absent languages yield nothing. Names
    are left unresolved — query time matches them to definitions.
    """
    pats = INHERIT.get(language)
    if not pats:
        return []
    edges: "list[Edge]" = []
    for i, line in enumerate(lines, start=1):
        for pat, kind in pats:
            m = pat.search(line)
            if not m:
                continue
            if kind == "_rust_impl":  # `impl Trait for Type` → Type implements Trait
                trait = _base_name(m.group(1))
                if trait:
                    edges.append(Edge(src=m.group(2), dst=trait,
                                      kind="implements", line=i))
                continue
            child = m.group(1)
            for base in m.group(2).split(","):
                bn = _base_name(base)
                if bn and bn != child:
                    edges.append(Edge(src=child, dst=bn, kind=kind, line=i))
    return edges


def extract_calls(lines, symbols) -> "list[Edge]":
    """Heuristic call edges: each call site attributed to its nearest enclosing
    definition (`src`), the callee name left unresolved (`dst`).

    Method calls `recv.name(` capture the receiver token (`recv`) — `self`/`this`
    or a variable — which the resolver later uses to bind the call to a specific
    definition. Free calls `name(` get an empty receiver. Approximate by design:
    language keywords and self-references are dropped, and (src, dst, recv) triples
    are deduped (first site kept) to bound size.
    """
    sorted_syms = sorted(symbols, key=lambda s: s.line)
    n = len(sorted_syms)
    edges: "list[Edge]" = []
    seen: "set[tuple]" = set()
    si = 0
    cur = ""  # enclosing symbol name; "" = file scope
    for i, line in enumerate(lines, start=1):
        while si < n and sorted_syms[si].line <= i:
            cur = sorted_syms[si].name
            si += 1
        # Method calls first (recv.name(...)), so we can record the receiver and
        # mark those spans handled; then free calls that aren't method tails.
        method_spans = []
        for m in _METHOD_CALL_RE.finditer(line):
            recv, name = m.group(1), m.group(2)
            method_spans.append(m.span(2))
            if name in _CALL_KEYWORDS or name == cur:
                continue
            key = (cur, name, recv)
            if key in seen:
                continue
            seen.add(key)
            edges.append(Edge(src=cur, dst=name, kind="calls", line=i, recv=recv))
        for m in _CALL_RE.finditer(line):
            name = m.group(1)
            if name in _CALL_KEYWORDS or name == cur or name.endswith("."):
                continue
            # skip if this `name(` is the tail of a method call already recorded
            if any(s0 <= m.start(1) < s1 for (s0, s1) in method_spans):
                continue
            key = (cur, name, "")
            if key in seen:
                continue
            seen.add(key)
            edges.append(Edge(src=cur, dst=name, kind="calls", line=i))
    return edges


def ctags_available() -> bool:
    """True if a *universal*-ctags binary is on PATH (memoized).

    Exuberant Ctags is rejected — it lacks `--output-format=json`. Any failure
    (binary absent, error) → False, silently, so callers fall back to regex.
    """
    global _CTAGS_OK
    if _CTAGS_OK is not None:
        return _CTAGS_OK
    try:
        out = subprocess.run(
            [_CTAGS_BIN or "ctags", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        _CTAGS_OK = "Universal Ctags" in out.stdout
    except (OSError, subprocess.SubprocessError):
        _CTAGS_OK = False
    return _CTAGS_OK


def _qualify(name: str, scope: str) -> str:
    """Qualify a method name with the last component of its ctags scope.

    `scope` may be nested with `::`, `.`, or `/` separators; we keep only the
    immediate owner (token-minimal, and the file/line already locate it). An
    empty or anonymous scope leaves the name unqualified.
    """
    if not scope:
        return name
    last = re.split(r"::|[./\\]", scope)[-1].strip()
    if not last or last.startswith("__anon"):
        return name
    return f"{last}.{name}"


def run_ctags(abs_paths, symbols_level: str) -> "dict[str, list[Symbol]]":
    """Batch-run universal-ctags over `abs_paths`; return {abs_path: [Symbol]}.

    One subprocess for the whole set (paths fed on stdin via `-L -`, sidestepping
    ARG_MAX). Parses newline-delimited JSON. Any failure → {} so each file falls
    back to regex. `symbols_level` controls which kinds survive (see _DEFAULT_KINDS).
    """
    if not abs_paths:
        return {}
    try:
        proc = subprocess.run(
            [_CTAGS_BIN or "ctags", "--quiet", "--output-format=json",
             "--fields=+nKzSe", "-f", "-", "-L", "-"],
            input="\n".join(abs_paths),
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    keep_members = symbols_level == "full"
    out: "dict[str, list[Symbol]]" = {}
    for raw in proc.stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue  # malformed line — skip, never abort the batch
        if rec.get("_type") != "tag":
            continue  # skip ptag/header lines
        path = rec.get("path")
        name = rec.get("name")
        line = rec.get("line")
        if not path or not name or not isinstance(line, int):
            continue
        scope = rec.get("scope") or ""
        scope_kind = (rec.get("scopeKind") or "").lower()
        kind = CTAGS_KIND_MAP.get((rec.get("kind") or "").lower(),
                                  (rec.get("kind") or "").lower())
        if not kind:
            continue
        # Languages like Python tag class methods as "member"; promote those to
        # "method" so they're qualified and kept at "defs". Struct/union members
        # (data fields) are NOT promoted — their scopeKind isn't a class.
        if kind == "member" and scope and scope_kind in _CLASS_SCOPE_KINDS:
            kind = "method"
        if kind not in _DEFAULT_KINDS and not keep_members:
            continue
        if kind in _METHOD_KINDS:
            name = _qualify(name, scope)
        end = rec.get("end")
        end = end if isinstance(end, int) and end >= line else 0
        out.setdefault(path, []).append(
            Symbol(name=name, kind=kind, line=line, end=end))

    for path, syms in out.items():
        # Dedupe on (name, kind) keeping the earliest line (proto+def, overloads),
        # then order by line so the index/map read top-to-bottom.
        best: "dict[tuple, Symbol]" = {}
        for s in syms:
            key = (s.name, s.kind)
            if key not in best or s.line < best[key].line:
                best[key] = s
        out[path] = sorted(best.values(), key=lambda s: s.line)
    return out


# --------------------------------------------------------------------------- #
# Optional tree-sitter backend (accurate symbols + call edges via real ASTs).
#
# Strictly opt-in (--tree-sitter): unlike ctags it is never auto-selected, so the
# default behavior — and the zero-dependency guarantee — is unchanged. When the
# `tree_sitter_language_pack` (or `tree_sitter_languages`) wheel is importable it
# replaces the symbol layer for supported languages and supplies precise call
# edges (exact enclosing scope + real call nodes, not `name(` heuristics).
# Inheritance edges and imports stay regex; unsupported languages fall back fully.
# --------------------------------------------------------------------------- #

# our language name -> tree-sitter grammar name.
TS_GRAMMAR = {
    "python": "python", "javascript": "javascript", "typescript": "typescript",
    "go": "go", "rust": "rust", "java": "java", "ruby": "ruby", "c": "c",
}
# .tsx / .jsx need the JSX-aware grammars.
TS_GRAMMAR_BY_EXT = {".tsx": "tsx", ".jsx": "javascript", ".mts": "typescript",
                     ".cts": "typescript"}

# AST node type -> our kind, per grammar.
TS_DEFS = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {"function_declaration": "function", "class_declaration": "class",
                   "method_definition": "method",
                   "generator_function_declaration": "function"},
    "typescript": {"function_declaration": "function", "class_declaration": "class",
                   "method_definition": "method", "interface_declaration": "interface",
                   "type_alias_declaration": "type", "enum_declaration": "enum",
                   "abstract_class_declaration": "class"},
    "go": {"function_declaration": "function", "method_declaration": "method",
           "type_spec": "type"},
    "rust": {"function_item": "function", "struct_item": "struct",
             "enum_item": "enum", "trait_item": "trait"},
    "java": {"class_declaration": "class", "interface_declaration": "interface",
             "enum_declaration": "enum", "method_declaration": "method"},
    "ruby": {"method": "function", "class": "class", "module": "module",
             "singleton_method": "function"},
    "c": {"function_definition": "function", "struct_specifier": "struct"},
}
TS_DEFS["tsx"] = TS_DEFS["typescript"]
# Node types that open a class scope (so nested functions qualify as Owner.method).
TS_CLASS = {
    "python": {"class_definition"}, "javascript": {"class_declaration"},
    "typescript": {"class_declaration", "abstract_class_declaration"},
    "tsx": {"class_declaration", "abstract_class_declaration"},
    "java": {"class_declaration", "interface_declaration", "enum_declaration"},
    "ruby": {"class", "module"}, "go": set(), "rust": set(), "c": set(),
}
# Call-expression node type per grammar, and the field naming the callee.
TS_CALL = {
    "python": ("call", "function"), "javascript": ("call_expression", "function"),
    "typescript": ("call_expression", "function"), "tsx": ("call_expression", "function"),
    "go": ("call_expression", "function"), "rust": ("call_expression", "function"),
    "java": ("method_invocation", "name"), "ruby": ("call", "method"),
    "c": ("call_expression", "function"),
}

_TS_BACKEND = None     # None=unprobed, False=unavailable, else the imported module
_TS_PARSERS: dict = {}  # grammar name -> parser (or None)


def _ts_backend():
    """The tree-sitter parser-factory module, or False if none is importable."""
    global _TS_BACKEND
    if _TS_BACKEND is not None:
        return _TS_BACKEND
    for modname in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            mod = __import__(modname)
        except Exception:
            continue
        if hasattr(mod, "get_parser"):
            _TS_BACKEND = mod
            return mod
    _TS_BACKEND = False
    return False


def tree_sitter_available() -> bool:
    return bool(_ts_backend())


def _ts_parser(grammar: str):
    if grammar in _TS_PARSERS:
        return _TS_PARSERS[grammar]
    mod = _ts_backend()
    parser = None
    if mod:
        try:
            parser = mod.get_parser(grammar)
        except Exception:
            parser = None
    _TS_PARSERS[grammar] = parser
    return parser


def _ts_grammar(language: str, ext: str):
    return TS_GRAMMAR_BY_EXT.get(ext) or TS_GRAMMAR.get(language)


_TS_IDENTS = {"identifier", "type_identifier", "field_identifier",
              "constant", "property_identifier"}

# tree-sitter Python bindings disagree on surface API: the mainstream wheel
# exposes node data as *properties* (node.type, node.start_point, node.children)
# while some Rust-backed builds expose them as *methods* (node.kind(),
# node.start_position(), node.child(i)). These accessors normalize both so the
# backend works regardless of which binding the user has installed.


def _ts_get(obj, name):
    v = getattr(obj, name, None)
    return v() if callable(v) else v


def _ts_kind(node) -> str:
    k = getattr(node, "type", None)
    if k is None:
        k = getattr(node, "kind", None)
    k = k() if callable(k) else k
    return k or ""


def _ts_children(node):
    ch = getattr(node, "children", None)
    if ch is not None and not callable(ch):
        return list(ch)
    cnt = _ts_get(node, "child_count") or 0
    return [node.child(i) for i in range(cnt)]


def _ts_named_children(node):
    ch = getattr(node, "named_children", None)
    if ch is not None and not callable(ch):
        return list(ch)
    cnt = _ts_get(node, "named_child_count") or 0
    return [node.named_child(i) for i in range(cnt)]


def _ts_row(node, names) -> int:
    for a in names:
        p = getattr(node, a, None)
        if p is None:
            continue
        if callable(p):
            p = p()
        if isinstance(p, (tuple, list)):
            return p[0]
        r = getattr(p, "row", None)
        if r is not None:
            return r
    return 0


def _ts_start(node):
    return _ts_row(node, ("start_point", "start_position"))


def _ts_end(node):
    return _ts_row(node, ("end_point", "end_position"))


def _ts_text(node, data: bytes) -> str:
    return data[_ts_get(node, "start_byte"):_ts_get(node, "end_byte")].decode(
        "utf-8", "replace")


def _ts_def_name(node, data: bytes) -> str:
    """Name of a definition node: its `name` field, else first identifier child."""
    nn = node.child_by_field_name("name")
    if nn is not None:
        return _ts_text(nn, data)
    # BFS for the first identifier — handles C declarators, Go type_spec, etc.
    queue = _ts_children(node)
    while queue:
        c = queue.pop(0)
        if _ts_kind(c) in _TS_IDENTS:
            return _ts_text(c, data)
        queue.extend(_ts_children(c))
    return ""


# Fields that, on a call/member node, name the receiver (object) part.
_TS_RECV_FIELDS = ("object", "receiver", "operand", "value")


def _ts_simple_ident(node, data: bytes) -> str:
    """Text of `node` if it's a bare identifier-ish token (self/this/super or a
    plain name), else "" — used to capture a *simple* call receiver only."""
    if node is None:
        return ""
    if _ts_kind(node) in _TS_IDENTS or _ts_kind(node) in (
            "this", "super", "self", "identifier"):
        txt = _ts_text(node, data)
        return txt if re.fullmatch(r"\w+", txt or "") else ""
    return ""


def _ts_rightmost(node, data: bytes) -> str:
    """Descend a member/scoped expression to its rightmost identifier
    (so `a.b.run()` and `pkg::run()` both yield `run`)."""
    target = node
    while _ts_get(target, "named_child_count"):
        kids = _ts_named_children(target)
        if not kids:
            break
        last = kids[-1]
        if _ts_kind(last) in _TS_IDENTS or _ts_kind(last) in (
                "member_expression", "scoped_identifier", "selector_expression",
                "field_expression", "scoped_type_identifier", "call"):
            target = last
        else:
            break
    txt = _ts_text(target, data)
    return txt if re.fullmatch(r"\w+", txt or "") else ""


def _ts_call_target(node, data: bytes, field: str):
    """(callee_name, receiver) for a call node.

    `receiver` is the simple object a method is called on — `self`/`this`/`super`
    or a bare variable name — or "" for a free function call or a complex
    receiver. It's what lets the resolver bind `self.run()` to the enclosing
    class's `run` rather than every `run` in the repo.
    """
    recv = ""
    # Languages whose call node carries an explicit receiver field (Java, Ruby).
    for rf in _TS_RECV_FIELDS:
        rn = node.child_by_field_name(rf)
        if rn is not None:
            recv = _ts_simple_ident(rn, data)
            break
    target = node.child_by_field_name(field)
    if target is None:
        return "", recv
    # Otherwise split a member-like callee `X.y` into receiver X + name y.
    if not recv and _ts_get(target, "named_child_count"):
        kids = _ts_named_children(target)
        if len(kids) >= 2:
            recv = _ts_simple_ident(kids[0], data)
    return _ts_rightmost(target, data), recv


def ts_extract(data: bytes, language: str, ext: str, symbols_level: str):
    """Parse `data` with tree-sitter; return (symbols, call_edges) or None.

    None means "no grammar for this file" → caller falls back to regex. Symbols
    carry exact start/end lines; methods are qualified Owner.method. Call edges
    are attributed to their exact enclosing definition.
    """
    grammar = _ts_grammar(language, ext)
    if not grammar:
        return None
    parser = _ts_parser(grammar)
    if parser is None:
        return None
    try:
        try:
            tree = parser.parse(data)                       # bytes (mainstream)
        except TypeError:
            tree = parser.parse(data.decode("utf-8", "replace"))  # str (Rust build)
    except Exception:
        return None
    root = getattr(tree, "root_node", None)
    if callable(root):
        root = root()
    if root is None:
        return None

    defs = TS_DEFS.get(grammar, {})
    class_nodes = TS_CLASS.get(grammar, set())
    call_type, call_field = TS_CALL.get(grammar, (None, None))
    keep_members = symbols_level == "full"
    symbols: "list[Symbol]" = []
    edges: "list[Edge]" = []

    def visit(node, enclosing: str, owner: str):
        new_enclosing, new_owner = enclosing, owner
        ntype = _ts_kind(node)
        kind = defs.get(ntype)
        if kind:
            try:
                name = _ts_def_name(node, data)
            except Exception:
                name = ""
            if name:
                k = "method" if (kind == "function" and owner) else kind
                disp = f"{owner}.{name}" if (k == "method" and owner) else name
                if k in _DEFAULT_KINDS or keep_members:
                    symbols.append(Symbol(name=disp, kind=k,
                                          line=_ts_start(node) + 1,
                                          end=_ts_end(node) + 1))
                new_enclosing = disp
                if ntype in class_nodes:
                    new_owner = name
        if call_type and ntype == call_type:
            try:
                callee, recv = _ts_call_target(node, data, call_field)
            except Exception:
                callee, recv = "", ""
            if callee and callee not in _CALL_KEYWORDS and callee != enclosing:
                edges.append(Edge(src=enclosing, dst=callee, kind="calls",
                                  line=_ts_start(node) + 1, recv=recv))
        for child in _ts_children(node):
            visit(child, new_enclosing, new_owner)

    try:
        visit(root, "", "")
    except RecursionError:
        return None

    # Dedupe symbols on (name, kind) keeping earliest; dedupe call edges on
    # (src, dst, recv) so calls with distinct receivers stay separate (the
    # receiver is what the resolver needs to disambiguate them).
    best: "dict[tuple, Symbol]" = {}
    for s in symbols:
        key = (s.name, s.kind)
        if key not in best or s.line < best[key].line:
            best[key] = s
    syms = sorted(best.values(), key=lambda s: s.line)
    seen: "set[tuple]" = set()
    uniq: "list[Edge]" = []
    for e in edges:
        key = (e.src, e.dst, e.recv)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return syms, uniq


def extract_leading_doc(lines, language: str) -> str:
    """Grab a short human-readable blurb from the top of the file.

    Handles a Python triple-quoted module docstring and runs of single-line
    comments (#, //). Returns at most the first ~3 non-empty lines.
    """
    # Skip a shebang and blank lines.
    idx = 0
    n = len(lines)
    while idx < n and (lines[idx].startswith("#!") or not lines[idx].strip()):
        idx += 1
    if idx >= n:
        return ""

    doc_lines: "list[str]" = []

    # Python module docstring.
    stripped = lines[idx].lstrip()
    if language == "python" and (stripped.startswith('"""') or stripped.startswith("'''")):
        quote = stripped[:3]
        body = stripped[3:]
        if body.endswith(quote) and len(body) >= 3:  # one-line docstring
            doc_lines.append(body[:-3].strip())
        else:
            if body.strip():
                doc_lines.append(body.strip())
            idx += 1
            while idx < n and quote not in lines[idx]:
                doc_lines.append(lines[idx].strip())
                idx += 1
    else:
        marker = LINE_COMMENT.get(language)
        if marker:
            while idx < n and lines[idx].lstrip().startswith(marker):
                text = lines[idx].lstrip()[len(marker):].strip()
                doc_lines.append(text)
                idx += 1

    doc_lines = [d for d in doc_lines if d]
    return " ".join(doc_lines[:3]).strip()


def analyze_bytes(rel: str, ext: str, data: bytes, digest: str,
                  symbols_override=None, symbols_level: str = "defs",
                  edges_enabled: bool = False,
                  call_edges_override=None) -> FileNode:
    """Build a FileNode from already-read bytes (binary already ruled out).

    `symbols_override` (a list) replaces the regex symbol layer when provided
    (ctags or tree-sitter result); `None` means "no precise result for this
    file" → regex fallback (also the backend-absent / backend-blind path).
    Imports + doc + inheritance edges are always regex. `call_edges_override`
    (from tree-sitter) replaces the heuristic `name(` call edges when given.
    `symbols_level="none"` suppresses symbols (and therefore edges).
    """
    lines = data.decode("utf-8", "replace").splitlines()
    language = EXT_TO_LANG.get(ext, "")
    node = FileNode(
        rel_path=rel, language=language, line_count=len(lines), content_hash=digest,
    )
    if language:
        cfg = LANGS[language]
        if symbols_level == "none":
            node.symbols = []
        elif symbols_override is not None:
            node.symbols = symbols_override
        else:
            node.symbols = extract_symbols(lines, cfg)
        node.imports = extract_imports(lines, cfg)
        node.doc = extract_leading_doc(lines, language)
        if edges_enabled and symbols_level != "none":
            assign_spans(node.symbols, len(lines))
            edges = extract_inherits(lines, language)
            if call_edges_override is not None:
                edges.extend(call_edges_override)
            else:
                edges.extend(extract_calls(lines, node.symbols))
            node.edges = edges
            node.import_bindings = extract_import_bindings(lines, language)
            node.var_types = extract_var_types(lines, language, node.symbols)
    return node


def read_file_bytes(path: Path):
    """Read a file's bytes if it's small enough and not binary.

    Returns (data, digest) or None when the file is skipped (too big, binary,
    or unreadable). Shared by the incremental triage in build_repo.
    """
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\0" in data[:4096]:  # binary
        return None
    return data, content_hash(data)


def process_file(path: Path, rel: str, cached: "FileNode | None",
                 symbols_override=None, symbols_level: str = "defs",
                 edges_enabled: bool = False, call_edges_override=None):
    """Return (node, reused) for one file, or (None, False) if skipped.

    Reads the file's bytes once and hashes them. If the hash matches a cached
    node, that node is reused verbatim (no re-analysis) — the incremental fast
    path. Otherwise the file is (re)analyzed (with precise symbols if supplied).
    """
    read = read_file_bytes(path)
    if read is None:
        return None, False
    data, digest = read
    if cached is not None and cached.content_hash == digest:
        return cached, True
    return analyze_bytes(rel, path.suffix.lower(), data, digest,
                         symbols_override, symbols_level, edges_enabled,
                         call_edges_override), False


def find_readme_summary(root: Path) -> str:
    """Title + first paragraph of the root README, as a one-line blurb."""
    for entry in sorted(root.iterdir() if root.is_dir() else []):
        if entry.is_file() and entry.name.lower().startswith("readme"):
            if is_binary(entry):
                continue
            # Group lines into paragraphs separated by blank lines; strip
            # Markdown heading markers. Keep the first two paragraphs (usually
            # the title and the opening blurb).
            paragraphs: "list[str]" = []
            current: "list[str]" = []
            for line in read_lines(entry):
                s = line.strip().lstrip("#").strip()
                if s:
                    current.append(s)
                elif current:
                    paragraphs.append(" ".join(current))
                    current = []
                if len(paragraphs) >= 2:
                    break
            if current and len(paragraphs) < 2:
                paragraphs.append(" ".join(current))
            summary = " ".join(paragraphs[:2]).strip()
            return summary[:300]
    return ""


def path_selected(rel: str, include, exclude) -> bool:
    """Apply include/exclude glob filters to a repo-relative path.

    include: if non-empty, the path must match at least one pattern.
    exclude: the path must match none of these patterns.
    Patterns are fnmatch globs against the POSIX relative path, e.g.
    'src/*', 'lib/std/*.zig', '*test*'.
    """
    if exclude and any(fnmatch.fnmatch(rel, pat) for pat in exclude):
        return False
    if include and not any(fnmatch.fnmatch(rel, pat) for pat in include):
        return False
    return True


def build_repo(root: Path, include=None, exclude=None, cache=None,
               use_ctags: bool = True, symbols_level: str = "defs",
               use_tree_sitter: bool = False, edges: bool = False) -> Repo:
    """Build the repo graph, reusing unchanged files from `cache` if given.

    `cache` is a dict {rel_path: FileNode} from a previous run (see
    load_cache). Files whose content hash matches are reused unchanged; the
    rest are analyzed. Deleted files simply don't appear in the result.

    Symbol backend, in precedence order: tree-sitter when `use_tree_sitter` and
    a grammar pack is importable (precise symbols + call edges), else
    universal-ctags when `use_ctags` and the binary is present, else regex. The
    chosen backend runs only over the *changed* files. Imports + docs (+ regex
    inheritance edges when `edges`) are always regex.
    """
    root = root.resolve()
    repo = Repo(root=root, name=root.name)
    repo.readme_summary = find_readme_summary(root)
    cache = cache or {}
    seen: "set[str]" = set()

    # git fast path: a tracked, clean file whose blob sha matches its cached
    # node is unchanged — reuse it without reading/hashing the file at all.
    blob = git_blob_shas(root)                 # None when not a git repo
    dirty = git_dirty_files(root) if blob is not None else set()

    # A cached node is only reusable if it was produced under the same profile
    # (symbol level + effective backend + edges flag); otherwise its symbols or
    # edges are stale, so we re-analyze. Resolved once and reused for Phase B.
    ts_effective = (use_tree_sitter and symbols_level != "none"
                    and tree_sitter_available())
    ctags_effective = (not ts_effective and use_ctags
                       and symbols_level != "none" and ctags_available())
    backend = "ts" if ts_effective else ("ct" if ctags_effective else "rx")
    # Edge tag is versioned ("e4" = + receivers, import bindings, var types) so
    # that bumping the edge schema invalidates only edge-enabled cache entries.
    want_key = f"{symbols_level}:{backend}:{'e4' if edges else '0'}"

    # Phase A — select, detect changes, reuse from cache. Defer analysis of
    # changed files so ctags can run over them in a single batch (Phase B).
    pending: "list[tuple]" = []  # (path, rel, ext, data, digest, gitsha)
    reused: "dict[str, FileNode]" = {}
    order: "list[str]" = []      # rel paths in walk order, for stable assembly
    for path in sorted(list_files(root)):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if not path_selected(rel, include, exclude):
            continue
        cached = cache.get(rel)
        reusable = cached is not None and cached.build_key == want_key
        cur_gitsha = blob.get(rel) if blob is not None else None

        if (reusable and cur_gitsha and rel not in dirty
                and cached.gitsha == cur_gitsha):
            seen.add(rel)
            order.append(rel)
            reused[rel] = cached
            continue  # unchanged per git, same profile — no file read

        read = read_file_bytes(path)
        if read is None:
            continue
        data, digest = read
        seen.add(rel)
        order.append(rel)
        if reusable and cached.content_hash == digest:
            if cur_gitsha:
                cached.gitsha = cur_gitsha  # learn blob sha for next-run fast path
            reused[rel] = cached
        else:
            pending.append((path, rel, path.suffix.lower(), data, digest,
                            cur_gitsha or ""))

    # Phase B — run the chosen precise backend over the changed files.
    # ctags: one batch subprocess. tree-sitter: per-file in-process parse
    # (also yields call edges). Both fall back to regex per file when blind.
    ctags_syms: "dict[str, list[Symbol]]" = {}
    ts_results: "dict[str, tuple]" = {}  # str(path) -> (symbols, call_edges)
    if ctags_effective:
        abs_for_ctags = [
            str(p) for (p, _rel, ext, _d, _h, _g) in pending if EXT_TO_LANG.get(ext)
        ]
        ctags_syms = run_ctags(abs_for_ctags, symbols_level)
    elif ts_effective:
        for path, _rel, ext, data, _d, _g in pending:
            if not EXT_TO_LANG.get(ext):
                continue
            res = ts_extract(data, EXT_TO_LANG[ext], ext, symbols_level)
            if res is not None:
                ts_results[str(path)] = res

    analyzed: "dict[str, FileNode]" = {}
    for path, rel, ext, data, digest, gitsha in pending:
        # A None override → backend absent or blind for this file → regex fallback.
        override = ctags_syms.get(str(path))
        call_override = None
        ts = ts_results.get(str(path))
        if ts is not None:
            override, call_override = ts[0], (ts[1] if edges else None)
        node = analyze_bytes(rel, ext, data, digest, override, symbols_level,
                             edges_enabled=edges, call_edges_override=call_override)
        node.gitsha = gitsha
        node.build_key = want_key
        analyzed[rel] = node

    for rel in order:
        if rel in reused:
            repo.files.append(reused[rel])
            repo.reused += 1
        else:
            repo.files.append(analyzed[rel])
            repo.analyzed += 1

    repo.dropped = sum(1 for rel in cache if rel not in seen)
    repo.files.sort(key=lambda f: f.rel_path)
    return repo


# --------------------------------------------------------------------------- #
# JSON (de)serialization — the cache *and* the machine-readable artifact.
# --------------------------------------------------------------------------- #


def repo_to_dict(repo: Repo) -> dict:
    def sym_dict(s):
        d = {"name": s.name, "kind": s.kind, "line": s.line}
        if s.end:  # omit unknown/zero ends to keep the artifact lean
            d["end"] = s.end
        return d

    def file_dict(f):
        d = {
            "path": f.rel_path,
            "language": f.language,
            "lines": f.line_count,
            "hash": f.content_hash,
            "doc": f.doc,
            "symbols": [sym_dict(s) for s in f.symbols],
            "imports": f.imports,
        }
        if f.edges:  # only present when built with --edges
            def edge_dict(e):
                ed = {"src": e.src, "dst": e.dst, "kind": e.kind, "line": e.line}
                if e.recv:
                    ed["recv"] = e.recv
                return ed
            d["edges"] = [edge_dict(e) for e in f.edges]
        if f.import_bindings:  # [(local, module)] — present only with --edges
            d["ibind"] = [[n, m] for (n, m) in f.import_bindings]
        if f.var_types:  # [(enclosing, var, type, line)] — present only with --edges
            d["vtypes"] = [[e, v, t, ln] for (e, v, t, ln) in f.var_types]
        if f.gitsha:  # omit when absent (non-git) to keep the artifact lean
            d["gitsha"] = f.gitsha
        if f.build_key:
            d["bk"] = f.build_key
        return d

    return {
        "repo": {"name": repo.name, "readme_summary": repo.readme_summary},
        "files": [file_dict(f) for f in repo.files],
    }


def files_from_dict(d: dict) -> "dict[str, FileNode]":
    """Reconstruct {rel_path: FileNode} from a serialized graph (cache load)."""
    out: "dict[str, FileNode]" = {}
    for fd in d.get("files", []):
        node = FileNode(
            rel_path=fd["path"],
            language=fd.get("language", ""),
            line_count=fd.get("lines", 0),
            content_hash=fd.get("hash", ""),
            gitsha=fd.get("gitsha", ""),
            build_key=fd.get("bk", ""),
            doc=fd.get("doc", ""),
            symbols=[
                Symbol(name=s["name"], kind=s["kind"], line=s["line"],
                       end=s.get("end", 0))
                for s in fd.get("symbols", [])
            ],
            imports=list(fd.get("imports", [])),
            edges=[
                Edge(src=e.get("src", ""), dst=e["dst"], kind=e["kind"],
                     line=e.get("line", 0), recv=e.get("recv", ""))
                for e in fd.get("edges", [])
            ],
            import_bindings=[tuple(b) for b in fd.get("ibind", [])],
            var_types=[tuple(v) for v in fd.get("vtypes", [])],
        )
        out[node.rel_path] = node
    return out


def load_cache(path: Path) -> "dict[str, FileNode]":
    try:
        return files_from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return {}  # missing or corrupt cache → full rebuild


def render_json(repo: Repo) -> str:
    """Compact JSON — agent-consumable and used as the incremental cache."""
    return json.dumps(repo_to_dict(repo), separators=(",", ":")) + "\n"


# --------------------------------------------------------------------------- #
# Rendering — Markdown repo map.
# --------------------------------------------------------------------------- #


def language_breakdown(repo: Repo):
    """Return [(language, file_count, line_count)] sorted by lines desc."""
    stats: "dict[str, list[int]]" = {}
    for f in repo.files:
        lang = f.language or "other"
        s = stats.setdefault(lang, [0, 0])
        s[0] += 1
        s[1] += f.line_count
    rows = [(lang, c[0], c[1]) for lang, c in stats.items()]
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows


def render_tree(repo: Repo) -> "list[str]":
    """Render an indented directory tree from the file list."""
    tree: dict = {}
    for f in repo.files:
        parts = f.rel_path.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append(parts[-1])

    out: "list[str]" = []

    def walk(node: dict, depth: int):
        for name in sorted(k for k in node if k != "__files__"):
            out.append(f"{'  ' * depth}- **{name}/**")
            walk(node[name], depth + 1)
        for fname in sorted(node.get("__files__", [])):
            out.append(f"{'  ' * depth}- {fname}")

    walk(tree, 0)
    return out


def render_markdown(repo: Repo) -> str:
    total_lines = sum(f.line_count for f in repo.files)
    total_symbols = sum(len(f.symbols) for f in repo.files)
    out: "list[str]" = []

    out.append(f"# Repo map: {repo.name}")
    out.append("")
    out.append(
        "_Generated by repograph. Symbol/import extraction is heuristic (regex, no "
        "real parsing) — treat it as an index, not ground truth._"
    )
    out.append("")

    # Overview
    out.append("## Overview")
    out.append("")
    out.append(f"- Files: **{len(repo.files)}**")
    out.append(f"- Lines: **{total_lines}**")
    out.append(f"- Symbols: **{total_symbols}**")
    if repo.readme_summary:
        out.append(f"- README: {repo.readme_summary}")
    out.append("")
    out.append("### Languages")
    out.append("")
    for lang, fc, lc in language_breakdown(repo):
        pct = (lc / total_lines * 100) if total_lines else 0
        out.append(f"- {lang}: {fc} files, {lc} lines ({pct:.0f}%)")
    out.append("")

    # Tree
    out.append("## Tree")
    out.append("")
    out.extend(render_tree(repo))
    out.append("")

    # Files
    out.append("## Files")
    out.append("")
    for f in repo.files:
        lang = f.language or "other"
        out.append(f"### [{f.rel_path}]({f.rel_path}) — {lang}, {f.line_count} lines")
        out.append("")
        if f.doc:
            out.append(f"> {f.doc}")
            out.append("")
        if f.symbols:
            out.append("Symbols:")
            out.append("")
            for s in f.symbols:
                # Link target already encodes path:line; no redundant trailer.
                out.append(f"- `{s.kind}` [{s.name}]({f.rel_path}#L{s.line})")
            out.append("")
        if f.imports:
            joined = ", ".join(f"`{imp}`" for imp in f.imports)
            out.append(f"Imports (heuristic): {joined}")
            out.append("")
        if f.edges:
            inherit = [e for e in f.edges if e.kind in ("extends", "implements")]
            calls = []
            seen_c = set()
            for e in f.edges:
                if e.kind == "calls" and e.dst not in seen_c:
                    seen_c.add(e.dst)
                    calls.append(e.dst)
            for e in inherit:
                out.append(f"- `{e.kind}` {e.src} → `{e.dst}`")
            if calls:
                shown = calls[:40]
                more = f" (+{len(calls) - 40} more)" if len(calls) > 40 else ""
                out.append("Calls (heuristic): "
                           + ", ".join(f"`{c}`" for c in shown) + more)
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def _group_symbols_for_index(symbols) -> "list[str]":
    """Render symbols as terse tokens, grouping methods under their owner.

    Top-level symbols render as `name:line`. Methods (kind "method", name like
    `Owner.member`) sharing an owner collapse into `Owner{m1:l1 m2:l2}` so the
    owner prefix is written once — if the owner is itself a symbol its line is
    kept (`Owner:1{...}`). Order follows the (line-sorted) symbol list.
    """
    methods: "dict[str, list[str]]" = {}
    for s in symbols:
        if s.kind == "method" and "." in s.name:
            owner, _, member = s.name.partition(".")
            methods.setdefault(owner, []).append(f"{member}:{s.line}")

    top_names = {s.name for s in symbols
                 if not (s.kind == "method" and "." in s.name)}

    tokens: "list[str]" = []
    emitted: "set[str]" = set()
    for s in symbols:
        if s.kind == "method" and "." in s.name:
            owner = s.name.partition(".")[0]
            if owner in emitted or owner in top_names:
                continue  # emitted with its owner symbol, or will be
            tokens.append(f"{owner}{{{' '.join(methods[owner])}}}")
            emitted.add(owner)
        elif s.name in methods and s.name not in emitted:
            tokens.append(f"{s.name}:{s.line}{{{' '.join(methods[s.name])}}}")
            emitted.add(s.name)
        else:
            tokens.append(f"{s.name}:{s.line}")
    return tokens


def render_index(repo: Repo) -> str:
    """Terse, maximally token-dense routing index: one line per file.

    Format per file:  path | lang | lines | name:line Owner{m:line ...} ...
    No docs, no imports, no links — just enough to route an LLM to a file and
    line, which it then opens directly. Smallest of the three formats.
    """
    out: "list[str]" = [
        f"# Repo index: {repo.name}",
        "# columns: path | lang | lines | symbols(name:line; Owner{m:line ...} groups methods)",
        "",
    ]
    for f in repo.files:
        syms = f.symbols
        overflow = 0
        if len(syms) > MAX_SYMBOLS_PER_FILE:
            overflow = len(syms) - MAX_SYMBOLS_PER_FILE
            syms = syms[:MAX_SYMBOLS_PER_FILE]
        tokens = _group_symbols_for_index(syms)
        if overflow:
            tokens.append(f"+{overflow} more")
        cell = " ".join(tokens)
        out.append(f"{f.rel_path} | {f.language or '-'} | {f.line_count} | {cell}".rstrip())
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Querying — lookups over a built Repo. Used by the CLI query flags and the MCP
# server. All structural (no embeddings) — exact/lexical, honest about limits.
# --------------------------------------------------------------------------- #

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize(name: str) -> "list[str]":
    """Split an identifier into lowercased word tokens.

    Handles camelCase, snake_case, kebab-case and dotted names, so e.g.
    `retryRequest`, `with_retries`, `Owner.retry` all yield a `retry` token —
    which is what lets `search` match a query by intent words, not exact name.
    """
    words: "list[str]" = []
    for part in _TOKEN_SPLIT.split(name):
        if part:
            words.extend(_CAMEL.sub(" ", part).split())
    return [w.lower() for w in words if w]


def _iter_symbols(repo: Repo):
    for f in repo.files:
        for s in f.symbols:
            yield f, s


def find_symbol(repo: Repo, query: str, kind=None, limit: int = 50):
    """Locate symbols by name (substring, case-insensitive), exact matches first.

    Returns a list of (rel_path, line, kind, name).
    """
    q = query.lower()
    rows = []
    for f, s in _iter_symbols(repo):
        if kind and s.kind != kind:
            continue
        if q in s.name.lower():
            rows.append((s.name.lower() != q, f.rel_path, s.line, s.kind, s.name))
    rows.sort()
    return [(r[1], r[2], r[3], r[4]) for r in rows[:limit]]


def search_symbols(repo: Repo, query: str, limit: int = 30):
    """Rank symbols by word-token overlap of the query with the symbol name and
    its file's leading doc — a lexical bridge toward "by intent" search (no
    embeddings). Returns (rel_path, line, kind, name, score), best first.
    """
    terms = set(tokenize(query))
    if not terms:
        return []
    ql = query.lower()
    scored = []
    for f, s in _iter_symbols(repo):
        name_hits = len(terms & set(tokenize(s.name)))
        doc_hits = len(terms & set(tokenize(f.doc))) if f.doc else 0
        score = name_hits * 3 + doc_hits
        if s.name.lower() == ql:
            score += 5
        elif not score and ql in s.name.lower():
            score = 1  # substring fallback (e.g. 'conn' → 'reconnect')
        if score > 0:
            scored.append((score, f.rel_path, s.line, s.kind, s.name))
    scored.sort(key=lambda r: (-r[0], r[1], r[2]))
    return [(r[1], r[2], r[3], r[4], r[0]) for r in scored[:limit]]


def find_refs(root, name: str, limit: int = 80, definitions=None):
    """Lexical usages of an identifier (word-boundary): git grep when available,
    else a stdlib scan over walked files.

    Approximate by design — this is "where does the name `X` appear", NOT a
    resolved call graph: it can't tell `a.connect()` from `b.connect()` and may
    include comments/strings. `definitions` is an optional set of (rel, line) to
    flag known definition sites. Returns (rel_path, line, is_def, text).
    """
    root = Path(root).resolve()
    definitions = definitions or set()
    rows = []
    used_git = False
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "grep", "-n", "-w", "--no-color", "-e", name],
                capture_output=True, text=True,
            )
            if out.returncode in (0, 1):  # 1 = no matches, still success
                used_git = True
                for line in out.stdout.splitlines():
                    p, _, rest = line.partition(":")
                    ln, _, text = rest.partition(":")
                    if ln.isdigit():
                        rows.append((p, int(ln), text.strip()))
        except OSError:
            pass
    if not used_git:
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        for path in list_files(root):
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            read = read_file_bytes(path)
            if read is None:
                continue
            for i, line in enumerate(read[0].decode("utf-8", "replace").splitlines(), 1):
                if pat.search(line):
                    rows.append((rel, i, line.strip()))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(rel, ln, (rel, ln) in definitions, text) for rel, ln, text in rows[:limit]]


def def_locations(repo: Repo) -> "set[tuple]":
    """{(rel_path, line)} of every known symbol definition — to flag defs in refs."""
    return {(f.rel_path, s.line) for f, s in _iter_symbols(repo)}


# --------------------------------------------------------------------------- #
# Relationship queries over edges — callers / callees / impact / affected.
#
# Edges are name-based and unresolved (see Edge); these resolve them against the
# global symbol table at query time. All lexical/structural — a call to `run` is
# matched to *every* `run` definition, so results are approximate (more precise
# with the tree-sitter backend, which records exact enclosing scopes). The map
# must be built with edges (--edges, or via the query commands which force it).
# --------------------------------------------------------------------------- #

# Paths that look like test files — used by impact/affected to flag test fallout.
_TEST_HINT = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?)(?:/|$)"   # a test/spec directory
    r"|(?:^|[/_.])(?:test_|_test|spec_|_spec)"     # name prefix/suffix tokens
    r"|\.(?:test|spec)\.",                          # foo.test.js / foo.spec.js
    re.IGNORECASE)


def is_test_path(rel: str) -> bool:
    return bool(_TEST_HINT.search(rel))


def _last(name: str) -> str:
    """Bare last component of a (possibly qualified) symbol name."""
    return name.rsplit(".", 1)[-1]


def symbol_index(repo: Repo) -> "dict[str, list]":
    """name -> [(rel_path, Symbol)], indexed by both full and bare-last name."""
    idx: "dict[str, list]" = {}
    for f in repo.files:
        for s in f.symbols:
            idx.setdefault(s.name, []).append((f.rel_path, s))
            bare = _last(s.name)
            if bare != s.name:
                idx.setdefault(bare, []).append((f.rel_path, s))
    return idx


def _resolve(idx, name: str):
    """Definition sites for a referenced name: exact, else bare-last match."""
    return idx.get(name) or idx.get(_last(name)) or []


# --------------------------------------------------------------------------- #
# Edge resolution — bind each name-based call edge to a *specific* definition,
# with a confidence level, instead of matching every same-named symbol.
#
# This runs as a separate in-memory pass over the assembled repo; it is NOT
# persisted into the per-file cache (which stays raw + per-file correct). Tiers,
# high → low confidence: self/super/typed-receiver (via the class hierarchy),
# same-file local def, then a global name fallback. The receiver token captured
# on each edge is what makes the high-confidence tiers possible.
# --------------------------------------------------------------------------- #

# Confidence ordering for filtering/sorting.
CONF_RANK = {"high": 3, "medium": 2, "low": 1, "external": 0}

# Receiver tokens that mean "the current instance/class".
_SELF_RECV = {"self", "this", "me", "cls", "Self"}

# Symbol kinds that define a class-like scope (own methods, can have bases).
_CLASS_KINDS_REG = {"class", "struct", "interface", "trait", "module",
                    "protocol", "object"}


@dataclass
class ResolvedEdge:
    src_file: str       # file the call is in
    src: str            # enclosing symbol (caller), "(file scope)" if none
    dst: str            # callee name (unqualified)
    recv: str           # receiver token at the call site
    call_line: int      # where the call appears
    dst_file: str = ""  # resolved definition file ("" = external/unresolved)
    dst_line: int = 0   # resolved definition line
    conf: str = "low"   # high | medium | low | external
    prov: str = "name"  # how it resolved: self/super/type/local/name-*/external
    ncand: int = 0      # candidate count at the name-fallback tier


def class_registry(repo: Repo) -> dict:
    """name -> {files, methods:{bare->(file,line)}, bases:set} over all classes.

    Methods come from qualified `Owner.method` symbols; bases from extends/
    implements edges. Keyed by bare class name globally (approximate but enough
    to walk an inheritance chain for method resolution).
    """
    classes: dict = {}

    def ci(name):
        return classes.setdefault(
            name, {"files": set(), "methods": {}, "bases": set()})

    for f in repo.files:
        for s in f.symbols:
            if "." in s.name and s.kind == "method":
                owner, _, m = s.name.rpartition(".")
                ci(owner)["methods"].setdefault(m, (f.rel_path, s.line))
            elif s.kind in _CLASS_KINDS_REG:
                ci(s.name)["files"].add(f.rel_path)
        for e in f.edges:
            if e.kind in ("extends", "implements"):
                ci(e.src)["bases"].add(e.dst)
    return classes


def _lookup_method(classes, cls, method, seen=None):
    """Find `method` on `cls` or, transitively, its bases. (file, line) or None."""
    seen = seen if seen is not None else set()
    if cls in seen:
        return None
    seen.add(cls)
    info = classes.get(cls)
    if not info:
        return None
    if method in info["methods"]:
        return info["methods"][method]
    for base in info["bases"]:
        hit = _lookup_method(classes, base, method, seen)
        if hit:
            return hit
    return None


_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")


def _posix_join_norm(base_dir: str, rel: str):
    """Normalize `rel` (which may contain ./ ../) against POSIX `base_dir`."""
    parts = base_dir.split("/") if base_dir else []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            else:
                return None  # escaped the repo root
        else:
            parts.append(seg)
    return "/".join(parts)


def _resolve_py_module(importer_rel: str, module: str, file_set) -> "str | None":
    """Resolve a Python import module ("a.b", ".a", "..a") to a repo file."""
    level = len(module) - len(module.lstrip("."))
    rest = module.lstrip(".")
    parts = rest.split(".") if rest else []
    if level == 0:
        base_parts = []                       # absolute: from the repo root
    else:
        imp_dir = importer_rel.rsplit("/", 1)[0] if "/" in importer_rel else ""
        base_parts = imp_dir.split("/") if imp_dir else []
        for _ in range(level - 1):            # each extra dot climbs a package
            if base_parts:
                base_parts.pop()
    base = "/".join(base_parts + parts)
    for cand in ((base + ".py", base + "/__init__.py") if base else ()):
        if cand in file_set:
            return cand
    return None


def _resolve_js_module(importer_rel: str, module: str, file_set) -> "str | None":
    """Resolve a relative JS/TS import ("./x", "../y/z") to a repo file (with
    extension + index resolution). Bare specifiers (node_modules) → None."""
    if not module.startswith("."):
        return None
    imp_dir = importer_rel.rsplit("/", 1)[0] if "/" in importer_rel else ""
    target = _posix_join_norm(imp_dir, module)
    if target is None:
        return None
    if target in file_set:
        return target
    for ext in _JS_EXTS:
        if target + ext in file_set:
            return target + ext
    for ext in _JS_EXTS:
        if target + "/index" + ext in file_set:
            return target + "/index" + ext
    return None


def _resolve_go_package(import_path: str, dir_files) -> "list[str]":
    """Resolve a Go import path to its package's repo files.

    No go.mod parsing: suffix-match the import path against repo directories
    (longest/most-specific suffix wins), so `github.com/me/proj/util` finds the
    repo dir `util` (or `proj/util`). External packages match nothing → [].
    """
    parts = [p for p in import_path.split("/") if p]
    for k in range(len(parts), 0, -1):
        suff = "/".join(parts[-k:])
        hits = [d for d in dir_files if d == suff or d.endswith("/" + suff)]
        if hits:
            return dir_files[min(hits, key=len)]
    return []


def _resolve_java_class(class_path: str, file_set) -> "list[str]":
    """Resolve a Java fully-qualified class (a.b.C) to its source file, suffix-
    matching so a source root prefix (src/main/java/…) is tolerated."""
    cand = class_path.replace(".", "/") + ".java"
    if cand in file_set:
        return [cand]
    suff = "/" + cand
    hits = [p for p in file_set if p.endswith(suff)]
    return [min(hits, key=len)] if hits else []


def _resolve_module_files(importer_rel, module, language, file_set,
                          dir_files) -> "list[str]":
    """Candidate repo file(s) an import refers to. Python/JS/Java resolve to one
    file; Go resolves to its package directory's files."""
    if language == "python":
        f = _resolve_py_module(importer_rel, module, file_set)
        return [f] if f else []
    if language in ("javascript", "typescript"):
        f = _resolve_js_module(importer_rel, module, file_set)
        return [f] if f else []
    if language == "go":
        return _resolve_go_package(module, dir_files)
    if language == "java":
        return _resolve_java_class(module, file_set)
    return []


def resolve_edges(repo: Repo) -> "list[ResolvedEdge]":
    """Resolve every call edge to a concrete definition with a confidence level."""
    classes = class_registry(repo)
    file_set = {f.rel_path for f in repo.files}
    lang_of = {f.rel_path: f.language for f in repo.files}
    bindings_of = {f.rel_path: dict(f.import_bindings) for f in repo.files}
    by_file: dict = {}   # (file, bare_name) -> [line, …]
    glob: dict = {}      # bare_name -> [(file, line), …]
    dir_files: dict = {}  # dir -> [files] (for Go package resolution)
    for f in repo.files:
        for s in f.symbols:
            bare = _last(s.name)
            by_file.setdefault((f.rel_path, bare), []).append(s.line)
            glob.setdefault(bare, []).append((f.rel_path, s.line))
        d = f.rel_path.rsplit("/", 1)[0] if "/" in f.rel_path else ""
        dir_files.setdefault(d, []).append(f.rel_path)
    # (file, enclosing, var) -> set of types from local assignments
    vartypes: dict = {}
    for f in repo.files:
        for (enclosing, var, typ, _ln) in f.var_types:
            vartypes.setdefault((f.rel_path, enclosing, var), set()).add(typ)

    def via_import(rf, name, dst):
        """Resolve `dst` through the file's import binding of `name` (the local
        name or receiver). Returns (file, line) or None."""
        mod = bindings_of.get(rf, {}).get(name)
        if not mod:
            return None
        for tf in _resolve_module_files(rf, mod, lang_of.get(rf, ""),
                                        file_set, dir_files):
            lines = by_file.get((tf, dst))
            if lines:
                return (tf, min(lines))
        return None

    out: "list[ResolvedEdge]" = []
    for f in repo.files:
        rf = f.rel_path
        for e in f.edges:
            if e.kind != "calls":
                continue
            r = ResolvedEdge(src_file=rf, src=e.src or "(file scope)",
                             dst=e.dst, recv=e.recv, call_line=e.line)
            owner = e.src.rpartition(".")[0] if "." in e.src else ""
            target = None

            # Tier 1a — self/this/cls: method on the enclosing class hierarchy.
            if e.recv in _SELF_RECV and owner:
                target = _lookup_method(classes, owner, e.dst)
                if target:
                    r.conf, r.prov = "high", "self"
            # Tier 1b — super: skip the current class, search its bases.
            elif e.recv == "super" and owner:
                for base in classes.get(owner, {}).get("bases", ()):
                    target = _lookup_method(classes, base, e.dst)
                    if target:
                        break
                if target:
                    r.conf, r.prov = "high", "super"
            # Tier 1c — receiver is an imported package/class (Go `util.Func()`,
            # Java `C.method()`, Python `mod.func()`): resolve via that import.
            # Tried before the class registry because the import names the exact
            # package, disambiguating same-named classes.
            if target is None and e.recv:
                target = via_import(rf, e.recv, e.dst)
                if target:
                    r.conf, r.prov = "high", "import"
            # Tier 1d — receiver is a known class name (Foo.bar() static-ish, or
            # a same-package class with no import).
            if target is None and e.recv and e.recv in classes:
                target = _lookup_method(classes, e.recv, e.dst)
                if target:
                    r.conf, r.prov = "high", "type"
            # Tier 1e — receiver is a local variable with a tracked type
            # (`x = Foo(); x.run()`): resolve the method on that type. High when
            # the variable's type is unambiguous in scope, else medium.
            if target is None and e.recv:
                types = vartypes.get((rf, e.src, e.recv))
                if types:
                    for t in sorted(types):
                        hit = _lookup_method(classes, t, e.dst)
                        if hit:
                            target = hit
                            r.conf = "high" if len(types) == 1 else "medium"
                            r.prov = "var-type"
                            break
            # Tier 2 — free call resolves to a same-file definition, else through
            # an import binding of the called name to the defining file.
            if target is None and not e.recv:
                local = by_file.get((rf, e.dst))
                if local:
                    target = (rf, min(local))
                    r.conf, r.prov = "high", "local"
                else:
                    target = via_import(rf, e.dst, e.dst)
                    if target:
                        r.conf, r.prov = "high", "import"
            # Tier 4 — global name fallback (today's behavior, now labeled).
            if target is None:
                cands = glob.get(e.dst, [])
                r.ncand = len(cands)
                if len(cands) == 1:
                    target, r.conf, r.prov = cands[0], "medium", "name-unique"
                elif len(cands) > 1:
                    target, r.conf, r.prov = cands[0], "low", "name-ambiguous"
                else:
                    r.conf, r.prov = "external", "external"

            if target:
                r.dst_file, r.dst_line = target
            out.append(r)
    return out


def _def_line_map(repo: Repo) -> dict:
    """(rel_path, symbol_name) -> earliest definition line."""
    m: dict = {}
    for f in repo.files:
        for s in f.symbols:
            key = (f.rel_path, s.name)
            if key not in m or s.line < m[key]:
                m[key] = s.line
    return m


def _def_sites(repo: Repo, name: str) -> "set[tuple]":
    """Definition (file, line) sites of NAME — exact name or bare-last match."""
    want = _last(name)
    sites = set()
    for f in repo.files:
        for s in f.symbols:
            if s.name == name or _last(s.name) == want:
                sites.add((f.rel_path, s.line))
    return sites


def find_callees(repo: Repo, name: str, limit: int = 100, min_conf: str = "low",
                 resolved=None):
    """Symbols called from within the definition(s) of NAME.

    Returns (callee, def_rel, def_line, call_line, conf); def_rel is "" for
    callees with no known definition in this repo (external/library calls).
    `min_conf` drops edges below a confidence floor (high|medium|low).
    """
    resolved = resolve_edges(repo) if resolved is None else resolved
    want = _last(name)
    floor = CONF_RANK.get(min_conf, 1)
    rows, seen = [], set()
    for r in resolved:
        if r.src != name and _last(r.src) != want:
            continue
        if r.conf != "external" and CONF_RANK[r.conf] < floor:
            continue
        key = (r.dst, r.dst_file, r.dst_line)
        if key in seen:
            continue
        seen.add(key)
        rows.append((r.dst, r.dst_file, r.dst_line, r.call_line, r.conf))
    rows.sort(key=lambda x: (x[1] == "", x[1], x[2], x[0]))
    return rows[:limit]


def find_callers(repo: Repo, name: str, limit: int = 100, min_conf: str = "low",
                 resolved=None):
    """Sites that call NAME, via *resolved* edges (so `a.run()`/`b.run()` no
    longer both match a `run` you didn't mean — at least at high confidence).

    Returns (rel_path, call_line, caller, caller_line, conf).
    """
    resolved = resolve_edges(repo) if resolved is None else resolved
    sites = _def_sites(repo, name)
    dlines = _def_line_map(repo)
    floor = CONF_RANK.get(min_conf, 1)
    rows, seen = [], set()
    for r in resolved:
        if (r.dst_file, r.dst_line) not in sites:
            continue
        if CONF_RANK[r.conf] < floor:
            continue
        cl = dlines.get((r.src_file, r.src), 0)
        key = (r.src_file, r.call_line, r.src)
        if key in seen:
            continue
        seen.add(key)
        rows.append((r.src_file, r.call_line, r.src, cl, r.conf))
    rows.sort(key=lambda x: (x[0], x[1]))
    return rows[:limit]


def find_impact(repo: Repo, name: str, max_depth: int = 3, limit: int = 200,
                min_conf: str = "low", resolved=None):
    """Transitive callers of NAME (the blast radius of changing it), over the
    *resolved* call graph. BFS up the reverse graph to `max_depth`.

    Returns (rows, files, test_files); rows are (rel, caller, call_line, depth).
    """
    resolved = resolve_edges(repo) if resolved is None else resolved
    dlines = _def_line_map(repo)
    floor = CONF_RANK.get(min_conf, 1)
    # reverse graph: a resolved def-site -> its callers (as def-sites we can expand)
    callers_of: "dict[tuple, list]" = {}
    for r in resolved:
        if not r.dst_file or CONF_RANK[r.conf] < floor:
            continue
        callers_of.setdefault((r.dst_file, r.dst_line), []).append(
            (r.src_file, r.src, r.call_line))

    visited, rows = set(), []
    frontier = [(site, 0) for site in _def_sites(repo, name)]
    seen_sites = set(frontier)
    while frontier:
        site, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for (sf, src, line) in callers_of.get(site, []):
            key = (sf, src, line)
            if key in visited:
                continue
            visited.add(key)
            rows.append((sf, src, line, depth + 1))
            caller_site = (sf, dlines.get((sf, src), 0))
            if caller_site[1] and caller_site not in seen_sites:
                seen_sites.add(caller_site)
                frontier.append((caller_site, depth + 1))
    rows.sort(key=lambda r: (r[3], r[0], r[2]))
    rows = rows[:limit]
    files = sorted({r[0] for r in rows})
    tests = [p for p in files if is_test_path(p)]
    return rows, files, tests


def file_dep_graph(repo: Repo) -> "dict[str, set]":
    """{rel_path: set(rel_paths it depends on)} from imports + call edges.

    Heuristic resolution: an import string is matched to a file by basename stem;
    a call edge is matched to the file(s) defining that symbol name.
    """
    sym2files: "dict[str, set]" = {}
    stem2file: "dict[str, set]" = {}
    for f in repo.files:
        for s in f.symbols:
            sym2files.setdefault(_last(s.name), set()).add(f.rel_path)
        stem = f.rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        stem2file.setdefault(stem, set()).add(f.rel_path)
    deps: "dict[str, set]" = {f.rel_path: set() for f in repo.files}
    for f in repo.files:
        for imp in f.imports:
            base = re.split(r"[\\/.]", imp.strip().rstrip("/"))[-1]
            for cand in stem2file.get(base, ()):
                if cand != f.rel_path:
                    deps[f.rel_path].add(cand)
        for e in f.edges:
            if e.kind == "calls":
                for cand in sym2files.get(_last(e.dst), ()):
                    if cand != f.rel_path:
                        deps[f.rel_path].add(cand)
    return deps


def find_affected(repo: Repo, changed, limit: int = 300):
    """Files transitively depending on `changed` (reverse import/call reach).

    Returns (test_files, other_files) — test files first since the common use is
    "which tests should I run after editing these files".
    """
    deps = file_dep_graph(repo)
    rev: "dict[str, set]" = {f.rel_path: set() for f in repo.files}
    for a, bs in deps.items():
        for b in bs:
            rev.setdefault(b, set()).add(a)
    changed = set(changed)
    impacted: "set[str]" = set()
    frontier = list(changed)
    while frontier:
        cur = frontier.pop()
        for dep in rev.get(cur, ()):
            if dep not in impacted and dep not in changed:
                impacted.add(dep)
                frontier.append(dep)
    impacted = set(list(impacted)[:limit])
    tests = sorted(p for p in impacted if is_test_path(p))
    others = sorted(p for p in impacted if not is_test_path(p))
    return tests, others


def git_changed_files(root: Path) -> "list[str]":
    """Changed paths vs HEAD (porcelain): staged + unstaged + untracked."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-z"],
            capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    changed = []
    for rec in out.stdout.decode("utf-8", "replace").split("\0"):
        if rec and len(rec) > 3:
            changed.append(rec[3:])
    return changed


def format_symbol_rows(rows) -> str:
    if not rows:
        return "(no matches)"
    return "\n".join(f"{r[0]}:{r[1]}  {r[2]}  {r[3]}" for r in rows)


def format_search_rows(rows) -> str:
    if not rows:
        return "(no matches)"
    return "\n".join(f"{r[0]}:{r[1]}  {r[2]}  {r[3]}  (score {r[4]})" for r in rows)


def format_ref_rows(rows) -> str:
    if not rows:
        return "(no references)"
    return "\n".join(
        f"{r[0]}:{r[1]}  {'[def] ' if r[2] else ''}{r[3]}" for r in rows
    )


def format_callee_rows(rows) -> str:
    if not rows:
        return "(no callees — built without --edges, or none found)"
    out = []
    for callee, dr, dl, call_line, conf in rows:
        tag = "" if conf == "high" else f" [{conf}]"
        if dr:
            out.append(f"{callee}  ->  {dr}:{dl}  (line {call_line}){tag}")
        else:
            out.append(f"{callee}  ->  (external)  (line {call_line})")
    return "\n".join(out)


def format_caller_rows(rows) -> str:
    if not rows:
        return "(no callers — built without --edges, or none found)"
    return "\n".join(
        f"{rel}:{line}  in {caller}"
        + (f" (def {rel}:{cl})" if cl else "")
        + ("" if conf == "high" else f" [{conf}]")
        for rel, line, caller, cl, conf in rows
    )


def format_impact_rows(result) -> str:
    rows, files, tests = result
    if not rows:
        return "(no impact — built without --edges, or nothing calls it)"
    out = [f"Impact: {len(files)} file(s), {len(tests)} test file(s) affected", ""]
    for rel, caller, line, depth in rows:
        out.append(f"{'  ' * (depth - 1)}{rel}:{line}  {caller}  (depth {depth})")
    if tests:
        out.append("")
        out.append("Affected tests: " + ", ".join(tests))
    return "\n".join(out)


def format_affected_rows(result) -> str:
    tests, others = result
    if not tests and not others:
        return "(no dependents found — built without --edges, or none)"
    out = [f"Affected: {len(tests)} test file(s), {len(others)} other file(s)", ""]
    if tests:
        out.append("Tests to run:")
        out.extend(f"  {p}" for p in tests)
    if others:
        out.append("Other dependents:")
        out.extend(f"  {p}" for p in others)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# explore / node — hand back SOURCE + call trails, not just a location.
#
# The other query modes (find/callers/…) route you to a `path:line` you then
# open. These return the verbatim source plus the calls around it, so an agent
# can answer "what does this do / how does it connect" in one call, with no
# follow-up file read. `node` is one symbol + its caller/callee trail; `explore`
# is a set of relevant symbols + the call paths *between* them.
# --------------------------------------------------------------------------- #

SOURCE_MAX_LINES = 80   # per-symbol source cap; longer bodies are elided.


def _files_by_rel(repo: Repo) -> dict:
    return {f.rel_path: f for f in repo.files}


def _symbol_span(fnode: FileNode, sym: Symbol):
    """(start, end, truncated) 1-based line span of SYM's source. Uses the
    recorded `end` when the backend knew it (tree-sitter/ctags); else falls back
    to the line before the next symbol in the file. Capped to SOURCE_MAX_LINES,
    with `truncated` flagging that the real body ran longer than shown."""
    start = sym.line
    if sym.end and sym.end >= start:
        raw_end = sym.end
    else:
        after = [s.line for s in fnode.symbols if s.line > start]
        raw_end = (min(after) - 1) if after else fnode.line_count
        if raw_end < start:
            raw_end = start
    end = min(raw_end, start + SOURCE_MAX_LINES - 1)
    return start, end, raw_end > end


def _render_source(repo: Repo, cache: dict, fnode: FileNode, sym: Symbol) -> str:
    """Line-numbered source slice for SYM. `cache` memoizes per-file reads."""
    rel = fnode.rel_path
    if rel not in cache:
        try:
            cache[rel] = read_lines(repo.root / rel)
        except OSError:
            cache[rel] = None
    lines = cache[rel]
    if lines is None:
        return f"    (could not read {rel})"
    start, end, truncated = _symbol_span(fnode, sym)
    end = min(end, len(lines))
    width = len(str(end))
    out = [f"  {str(n).rjust(width)} | {lines[n - 1]}" for n in range(start, end + 1)]
    if truncated:
        out.append(f"  {'…'.rjust(width)} | … (elided; open {rel}:{start} for the rest)")
    return "\n".join(out)


def _find_symbol_object(fnode, line, name):
    """The Symbol at LINE named NAME in FNODE (or the first at LINE)."""
    if not fnode:
        return None
    exact = next((s for s in fnode.symbols if s.line == line and s.name == name), None)
    return exact or next((s for s in fnode.symbols if s.line == line), None)


def node(repo: Repo, name: str, max_defs: int = 3, min_conf: str = "low",
         resolved=None) -> str:
    """One symbol's verbatim source plus its caller/callee trail."""
    frel = _files_by_rel(repo)
    hits = find_symbol(repo, name)
    want = _last(name)
    exact = [h for h in hits if h[3] == name or _last(h[3]) == want]
    hits = exact or hits
    if not hits:
        return f"(no symbol matching {name!r})"
    resolved = resolve_edges(repo) if resolved is None else resolved
    cache: dict = {}
    out = []
    for rel, line, kind, sym_name in hits[:max_defs]:
        fnode = frel.get(rel)
        sym = _find_symbol_object(fnode, line, sym_name)
        out.append(f"== {sym_name}  ({kind})  {rel}:{line}")
        if sym is not None:
            out.append(_render_source(repo, cache, fnode, sym))
        callees = find_callees(repo, sym_name, min_conf=min_conf, resolved=resolved)
        if callees:
            out.append("  calls:")
            for callee, dr, dl, cl, conf in callees[:15]:
                loc = f"{dr}:{dl}" if dr else "(external)"
                tag = "" if conf == "high" else f" [{conf}]"
                out.append(f"    -> {callee}  {loc}{tag}")
        callers = find_callers(repo, sym_name, min_conf=min_conf, resolved=resolved)
        if callers:
            out.append("  called by:")
            for crel, cline, caller, cl, conf in callers[:15]:
                tag = "" if conf == "high" else f" [{conf}]"
                out.append(f"    <- {caller}  {crel}:{cline}{tag}")
        out.append("")
    return "\n".join(out).rstrip() or f"(no symbol matching {name!r})"


def explore(repo: Repo, query: str, max_files: int = 8, min_conf: str = "low",
            resolved=None) -> str:
    """Relevant symbols' source + the resolved call paths between them.

    Resolves QUERY to symbols two ways — exact lookups for each identifier-like
    token, then lexical `search` ranking to fill the rest — then prints each
    one's source and the call edges that run *between* the selected symbols.
    """
    frel = _files_by_rel(repo)
    picked: list = []
    seen: set = set()

    def add(rel, line, kind, nm):
        if (rel, line) not in seen:
            seen.add((rel, line))
            picked.append((rel, line, kind, nm))

    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_.]+", query):
        for rel, line, kind, nm in find_symbol(repo, tok, limit=5):
            if nm.lower() == tok.lower() or _last(nm).lower() == _last(tok).lower():
                add(rel, line, kind, nm)
    for rel, line, kind, nm, _score in search_symbols(repo, query, limit=max_files * 3):
        add(rel, line, kind, nm)
    picked = picked[:max_files]
    if not picked:
        return f"(nothing relevant to {query!r})"

    resolved = resolve_edges(repo) if resolved is None else resolved
    cache: dict = {}
    out = [f"Explore: {query}", f"{len(picked)} relevant symbol(s):", ""]
    for rel, line, kind, nm in picked:
        fnode = frel.get(rel)
        sym = _find_symbol_object(fnode, line, nm)
        out.append(f"== {nm}  ({kind})  {rel}:{line}")
        if sym is not None:
            out.append(_render_source(repo, cache, fnode, sym))
        out.append("")

    sites = {(rel, line) for rel, line, _, _ in picked}
    names = {_last(nm) for _, _, _, nm in picked}
    floor = CONF_RANK.get(min_conf, 1)
    paths = set()
    for r in resolved:
        if CONF_RANK.get(r.conf, 0) < floor:
            continue
        if (r.dst_file, r.dst_line) in sites and _last(r.src) in names:
            tag = "" if r.conf == "high" else f" [{r.conf}]"
            paths.add(f"  {r.src}  ->  {r.dst}  ({r.src_file}:{r.call_line}){tag}")
    if paths:
        out.append("Call paths between these symbols:")
        out.extend(sorted(paths))
    return "\n".join(out).rstrip()


# --------------------------------------------------------------------------- #
# init — scaffold the committed-map integration into a consuming repo.
# --------------------------------------------------------------------------- #

# A portable (POSIX sh) pre-commit hook. It resolves the repograph command at
# *commit time* — preferring the consumer's installed npm bin — so it works in
# any repo that depends on repograph regardless of where node_modules lives, and
# no-ops cleanly when the tool isn't installed yet (e.g. a fresh clone before
# `npm install`). It refreshes the committed map and stages it, so .repograph/
# travels with each commit.
HOOK_TEMPLATE = """\
#!/bin/sh
# >>> repograph >>>
# Managed by `repograph init` — refreshes the committed repo map and stages it.
# No-op if repograph isn't installed, so it never blocks a commit.
set -e
ROOT=$(git rev-parse --show-toplevel)
if   [ -n "${{REPOGRAPH:-}}" ]; then RG="$REPOGRAPH"
elif [ -x "$ROOT/node_modules/.bin/repograph" ]; then RG="$ROOT/node_modules/.bin/repograph"
elif command -v repograph >/dev/null 2>&1; then RG=repograph
else exit 0
fi
"$RG" "$ROOT" -f index -o "$ROOT/.repograph/index.txt" --cache "$ROOT/.repograph/graph.json"{scope} >/dev/null 2>&1 || exit 0
"$RG" "$ROOT" -f md    -o "$ROOT/.repograph/map.md"    --cache "$ROOT/.repograph/graph.json"{scope} >/dev/null 2>&1 || exit 0
git add "$ROOT/.repograph" 2>/dev/null || true
# <<< repograph <<<
"""


def _scope_flags(include, exclude, edges=False, use_tree_sitter=False) -> str:
    """Render the build profile as shell-quoted CLI flags to bake into the hook,
    so refreshes use the same scope as `init`. Leading space when non-empty."""
    parts = []
    for g in include or []:
        parts.append("--include " + shlex.quote(g))
    for g in exclude or []:
        parts.append("--exclude " + shlex.quote(g))
    if edges:
        parts.append("--edges")
    if use_tree_sitter:
        parts.append("--tree-sitter")
    return (" " + " ".join(parts)) if parts else ""


def cmd_init(repo: Path, include, exclude, symbols_level: str,
             use_ctags: bool, use_tree_sitter: bool = False,
             edges: bool = False) -> int:
    """Scaffold the committed-map workflow into `repo`:

      1. build the initial .repograph/ map (index.txt + map.md + graph.json),
      2. write a tracked .githooks/pre-commit that refreshes + stages it,
      3. activate it locally (core.hooksPath), and print how to make it
         auto-activate for everyone who clones.
    """
    repo = repo.resolve()
    if not (repo / ".git").exists():
        print(f"repograph init: {repo} is not a git repo", file=sys.stderr)
        return 1

    # 1. Initial map — reuse an existing cache if one is already there.
    outdir = repo / ".repograph"
    outdir.mkdir(exist_ok=True)
    cache_path = outdir / "graph.json"
    cache = load_cache(cache_path) if cache_path.exists() else {}
    graph = build_repo(repo, include=include, exclude=exclude, cache=cache,
                       use_ctags=use_ctags, symbols_level=symbols_level,
                       use_tree_sitter=use_tree_sitter, edges=edges)
    (outdir / "index.txt").write_text(render_index(graph), encoding="utf-8")
    (outdir / "map.md").write_text(render_markdown(graph), encoding="utf-8")
    cache_path.write_text(render_json(graph), encoding="utf-8")

    # 2. Tracked pre-commit hook with the scope baked in.
    hooks = repo / ".githooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(
        HOOK_TEMPLATE.format(
            scope=_scope_flags(include, exclude, edges, use_tree_sitter)),
        encoding="utf-8")
    hook.chmod(0o755)

    # 3. Activate for the current clone.
    activated = False
    try:
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath",
                        ".githooks"], check=True, capture_output=True)
        activated = True
    except (OSError, subprocess.CalledProcessError):
        pass

    # 4. Report + how to make it auto-activate on every clone.
    n = len(graph.files)
    print(f"repograph init: wrote .repograph/ ({n} files) + .githooks/pre-commit",
          file=sys.stderr)
    print("  core.hooksPath -> .githooks " +
          ("(set)" if activated else "(set it manually: git config core.hooksPath .githooks)"),
          file=sys.stderr)
    if (repo / "package.json").exists():
        print('  to auto-activate after clone, add to package.json scripts:\n'
              '    "prepare": "git config core.hooksPath .githooks"', file=sys.stderr)
    print("  commit .repograph/ and .githooks/ so the map travels with the repo.",
          file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# watch — rebuild on change. Stdlib only: poll mtimes, rebuild incrementally.
# --------------------------------------------------------------------------- #


def _repo_signature(root: Path) -> dict:
    """{abs_path: mtime_ns} over the repo's files — a cheap change fingerprint."""
    sig: dict = {}
    for p in list_files(root):
        try:
            if p.is_file():
                sig[str(p)] = p.stat().st_mtime_ns
        except OSError:
            pass
    return sig


def cmd_watch(args, need_edges: bool) -> int:
    """Rebuild the map incrementally whenever a watched file changes.

    Polls file mtimes every --interval seconds (no third-party watcher, keeping
    the zero-dependency promise); each change triggers an incremental build that
    rewrites the output and refreshes the cache. Runs until interrupted.
    """
    root = args.repo.resolve()
    render = RENDERERS[args.format]

    def rebuild() -> Repo:
        cache = {}
        if args.cache and args.cache.exists():
            cache = load_cache(args.cache)
        repo = build_repo(
            root, include=args.include, exclude=args.exclude, cache=cache,
            use_ctags=not args.no_ctags, symbols_level=args.symbols,
            use_tree_sitter=args.tree_sitter, edges=need_edges,
        )
        if args.cache:
            args.cache.write_text(render_json(repo), encoding="utf-8")
        rendered = render(repo)
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return repo

    repo = rebuild()
    dest = str(args.output) if args.output else "stdout"
    print(f"repograph watch: built {len(repo.files)} files -> {dest}; "
          f"polling every {args.interval}s (Ctrl-C to stop)", file=sys.stderr)
    prev = _repo_signature(root)
    try:
        while True:
            time.sleep(max(0.1, args.interval))
            cur = _repo_signature(root)
            if cur != prev:
                prev = cur
                r = rebuild()
                print(f"repograph watch: {r.analyzed} changed, {r.reused} reused, "
                      f"{r.dropped} removed", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nrepograph watch: stopped", file=sys.stderr)
        return 0


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


RENDERERS = {"md": render_markdown, "json": render_json, "index": render_index}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a knowledge-graph / repo-map of a local repo.",
    )
    parser.add_argument(
        "repo", type=Path, nargs="?", default=Path("."),
        help="path to the repo directory (default: current directory)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="write here (default: stdout)",
    )
    parser.add_argument(
        "-f", "--format", choices=list(RENDERERS), default="md",
        help="output format: md (default), json (compact, agent-friendly), "
             "index (terse one-line-per-file routing index)",
    )
    parser.add_argument(
        "--include", action="append", metavar="GLOB",
        help="only include paths matching this glob (repeatable), e.g. 'src/*'",
    )
    parser.add_argument(
        "--exclude", action="append", metavar="GLOB",
        help="drop paths matching this glob (repeatable), e.g. 'lib/libc/*'",
    )
    parser.add_argument(
        "--cache", type=Path, default=None, metavar="FILE",
        help="JSON graph cache for incremental update: unchanged files (by "
             "content hash) are reused, only changed ones re-analyzed",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="ignore any existing --cache and analyze every file",
    )
    parser.add_argument(
        "--symbols", choices=["none", "defs", "full"], default="defs",
        help="symbol detail: none (smallest), defs (default: top-level defs + "
             "qualified methods), full (also members/fields — bigger)",
    )
    parser.add_argument(
        "--no-ctags", action="store_true",
        help="force the regex extractor even if universal-ctags is installed "
             "(ctags is used automatically when present)",
    )
    parser.add_argument(
        "--ctags-path", metavar="BIN", default=None,
        help="path to the universal-ctags binary (default: 'ctags' on PATH)",
    )
    parser.add_argument(
        "--tree-sitter", action="store_true",
        help="use the optional tree-sitter backend for precise symbols + call "
             "edges (needs the tree_sitter_language_pack wheel; opt-in)",
    )
    parser.add_argument(
        "--edges", action="store_true",
        help="extract relationship edges (calls, extends, implements) and "
             "include them in the map/json (the call-graph queries force this on)",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="rebuild incrementally whenever a file changes (until Ctrl-C); "
             "pairs with -o and --cache",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, metavar="SEC",
        help="--watch poll interval in seconds (default: 1.0)",
    )
    q = parser.add_argument_group("query modes (print lookups instead of a map)")
    q.add_argument("--find", metavar="NAME",
                   help="print where symbols matching NAME are defined (path:line)")
    q.add_argument("--search", metavar="QUERY",
                   help="rank symbols by word/doc overlap with QUERY (lexical)")
    q.add_argument("--refs", metavar="NAME",
                   help="print lexical usages of NAME (git grep; approximate)")
    q.add_argument("--callers", metavar="NAME",
                   help="print call sites of NAME (needs edges; approximate)")
    q.add_argument("--callees", metavar="NAME",
                   help="print symbols called from within NAME (needs edges)")
    q.add_argument("--impact", metavar="NAME",
                   help="print the transitive callers of NAME — its blast radius")
    q.add_argument("--node", metavar="NAME",
                   help="print one symbol's SOURCE + its caller/callee trail "
                        "(needs edges)")
    q.add_argument("--explore", metavar="QUERY",
                   help="print relevant symbols' SOURCE + the call paths between "
                        "them, in one shot (names or a question; needs edges)")
    q.add_argument("--max-files", type=int, default=8, dest="max_files",
                   metavar="N",
                   help="--explore: max symbols to include source for (default 8)")
    q.add_argument("--affected", nargs="?", const="", metavar="FILES",
                   help="print files/tests depending on FILES (comma-separated; "
                        "omit the value to use git's changed files)")
    q.add_argument("--min-confidence", choices=["low", "medium", "high"],
                   default="low", dest="min_confidence",
                   help="drop resolved call edges below this confidence in "
                        "callers/callees/impact (default: low = keep all)")
    q.add_argument("--strict", action="store_true",
                   help="shorthand for --min-confidence high (only confidently "
                        "resolved call edges)")
    parser.add_argument(
        "--init", action="store_true",
        help="scaffold the committed-map workflow into REPO: write .repograph/, "
             "a tracked .githooks/pre-commit that refreshes+stages it, and "
             "activate it. Respects --include/--exclude/--symbols (baked into "
             "the hook).",
    )
    args = parser.parse_args(argv)

    if not args.repo.is_dir():
        print(f"error: {args.repo} is not a directory", file=sys.stderr)
        return 2

    global _CTAGS_BIN
    if args.ctags_path:
        _CTAGS_BIN = args.ctags_path

    if args.init:
        return cmd_init(args.repo, include=args.include, exclude=args.exclude,
                        symbols_level=args.symbols, use_ctags=not args.no_ctags,
                        use_tree_sitter=args.tree_sitter, edges=args.edges)

    # The relationship queries need edges; force them on for those runs even if
    # --edges wasn't passed (the persisted artifact still honors --edges).
    edge_query = any(x is not None for x in
                     (args.callers, args.callees, args.impact, args.affected,
                      args.node, args.explore))
    need_edges = args.edges or edge_query

    if args.tree_sitter and not tree_sitter_available():
        print("warning: --tree-sitter requested but no tree_sitter_language_pack "
              "/ tree_sitter_languages wheel is importable; falling back",
              file=sys.stderr)

    if args.watch:
        return cmd_watch(args, need_edges)

    cache = {}
    if args.cache and args.cache.exists() and not args.rebuild:
        cache = load_cache(args.cache)

    repo = build_repo(
        args.repo, include=args.include, exclude=args.exclude, cache=cache,
        use_ctags=not args.no_ctags, symbols_level=args.symbols,
        use_tree_sitter=args.tree_sitter, edges=need_edges,
    )

    # Persist/refresh the cache as JSON (same schema as --format json). Skip when
    # a query forced edges on but --edges wasn't asked for, so the persisted
    # cache keeps the user's chosen (edge-free) profile.
    if args.cache and (args.edges or not edge_query):
        args.cache.write_text(render_json(repo), encoding="utf-8")

    # Query modes short-circuit: print the lookup, not a map.
    if args.find is not None:
        print(format_symbol_rows(find_symbol(repo, args.find)))
        return 0
    if args.search is not None:
        print(format_search_rows(search_symbols(repo, args.search)))
        return 0
    if args.refs is not None:
        print(format_ref_rows(find_refs(repo.root, args.refs,
                                        definitions=def_locations(repo))))
        return 0
    min_conf = "high" if args.strict else args.min_confidence
    if args.callers is not None:
        print(format_caller_rows(find_callers(repo, args.callers, min_conf=min_conf)))
        return 0
    if args.callees is not None:
        print(format_callee_rows(find_callees(repo, args.callees, min_conf=min_conf)))
        return 0
    if args.impact is not None:
        print(format_impact_rows(find_impact(repo, args.impact, min_conf=min_conf)))
        return 0
    if args.affected is not None:
        changed = ([c.strip() for c in args.affected.split(",") if c.strip()]
                   if args.affected else git_changed_files(repo.root))
        print(format_affected_rows(find_affected(repo, changed)))
        return 0
    if args.node is not None:
        print(node(repo, args.node, min_conf=min_conf))
        return 0
    if args.explore is not None:
        print(explore(repo, args.explore, max_files=args.max_files,
                      min_conf=min_conf))
        return 0

    rendered = RENDERERS[args.format](repo)

    if cache:
        print(
            f"Updated: {repo.analyzed} changed, {repo.reused} reused, "
            f"{repo.dropped} removed", file=sys.stderr,
        )

    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.output} ({len(repo.files)} files, {args.format})", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
