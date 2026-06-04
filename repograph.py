#!/usr/bin/env python3
"""repograph — build a language-agnostic knowledge graph of a local repo.

Point it at a directory and it emits a Markdown "repo map": an overview, a
directory tree, and a per-file breakdown of symbols (functions/types/classes),
leading docs, and imports. Every file and symbol links back to the original
source as `path#Lnn`, so the map doubles as an index into the code.

Extraction is deliberately *shallow and heuristic* — regex per language, no real
parsing — which is what makes it work on any repo with zero dependencies. It will
miss and mis-tag some things, especially imports; sections say so.

Usage:
    python repograph.py <repo-path> [-o REPOMAP.md]

Standard library only. Python 3.8+.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
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
    line: int  # 1-based


@dataclass
class FileNode:
    rel_path: str          # POSIX-style, relative to repo root
    language: str          # language name or "" if unknown
    line_count: int
    content_hash: str = "" # sha1 of file bytes — drives incremental update
    gitsha: str = ""       # git blob sha (when tracked) — git-status fast path
    build_key: str = ""    # "<symbols_level>:<ctags?>" — invalidates reuse on change
    doc: str = ""          # leading comment/docstring, trimmed
    symbols: list = field(default_factory=list)   # list[Symbol]
    imports: list = field(default_factory=list)   # list[str]


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
             "--fields=+nKzS", "-f", "-", "-L", "-"],
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
        out.setdefault(path, []).append(Symbol(name=name, kind=kind, line=line))

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
                  ctags_symbols=None, symbols_level: str = "defs") -> FileNode:
    """Build a FileNode from already-read bytes (binary already ruled out).

    `ctags_symbols` (a list) replaces the regex symbol layer when provided;
    `None` means "no ctags result for this file" → fall back to regex (this is
    also the ctags-absent and ctags-blind path). Imports + doc are always regex.
    `symbols_level="none"` suppresses symbols entirely.
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
        elif ctags_symbols is not None:
            node.symbols = ctags_symbols
        else:
            node.symbols = extract_symbols(lines, cfg)
        node.imports = extract_imports(lines, cfg)
        node.doc = extract_leading_doc(lines, language)
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
                 ctags_symbols=None, symbols_level: str = "defs"):
    """Return (node, reused) for one file, or (None, False) if skipped.

    Reads the file's bytes once and hashes them. If the hash matches a cached
    node, that node is reused verbatim (no re-analysis) — the incremental fast
    path. Otherwise the file is (re)analyzed (with ctags symbols if supplied).
    """
    read = read_file_bytes(path)
    if read is None:
        return None, False
    data, digest = read
    if cached is not None and cached.content_hash == digest:
        return cached, True
    return analyze_bytes(rel, path.suffix.lower(), data, digest,
                         ctags_symbols, symbols_level), False


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
               use_ctags: bool = True, symbols_level: str = "defs") -> Repo:
    """Build the repo graph, reusing unchanged files from `cache` if given.

    `cache` is a dict {rel_path: FileNode} from a previous run (see
    load_cache). Files whose content hash matches are reused unchanged; the
    rest are analyzed. Deleted files simply don't appear in the result.

    When `use_ctags` and a universal-ctags binary is present, symbols for the
    *changed* files are extracted by one batch ctags call; otherwise (or for
    files ctags can't handle) the regex extractor is used. Imports + docs are
    always regex.
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

    # A cached node is only reusable if it was produced under the same symbol
    # profile (level + effective backend); otherwise its symbols are stale, so
    # we re-analyze. ctags_effective is computed once and reused for Phase B.
    ctags_effective = (use_ctags and symbols_level != "none" and ctags_available())
    want_key = f"{symbols_level}:{int(ctags_effective)}"

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

    # Phase B — one batch ctags call over the changed files with a known language.
    ctags_syms: "dict[str, list[Symbol]]" = {}
    if ctags_effective:
        abs_for_ctags = [
            str(p) for (p, _rel, ext, _d, _h, _g) in pending if EXT_TO_LANG.get(ext)
        ]
        ctags_syms = run_ctags(abs_for_ctags, symbols_level)

    analyzed: "dict[str, FileNode]" = {}
    for path, rel, ext, data, digest, gitsha in pending:
        # A None lookup → ctags absent or blind for this file → regex fallback.
        cs = ctags_syms.get(str(path))
        node = analyze_bytes(rel, ext, data, digest, cs, symbols_level)
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
    def file_dict(f):
        d = {
            "path": f.rel_path,
            "language": f.language,
            "lines": f.line_count,
            "hash": f.content_hash,
            "doc": f.doc,
            "symbols": [
                {"name": s.name, "kind": s.kind, "line": s.line} for s in f.symbols
            ],
            "imports": f.imports,
        }
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
            symbols=[Symbol(**s) for s in fd.get("symbols", [])],
            imports=list(fd.get("imports", [])),
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
    q = parser.add_argument_group("query modes (print lookups instead of a map)")
    q.add_argument("--find", metavar="NAME",
                   help="print where symbols matching NAME are defined (path:line)")
    q.add_argument("--search", metavar="QUERY",
                   help="rank symbols by word/doc overlap with QUERY (lexical)")
    q.add_argument("--refs", metavar="NAME",
                   help="print lexical usages of NAME (git grep; approximate)")
    args = parser.parse_args(argv)

    if not args.repo.is_dir():
        print(f"error: {args.repo} is not a directory", file=sys.stderr)
        return 2

    global _CTAGS_BIN
    if args.ctags_path:
        _CTAGS_BIN = args.ctags_path

    cache = {}
    if args.cache and args.cache.exists() and not args.rebuild:
        cache = load_cache(args.cache)

    repo = build_repo(
        args.repo, include=args.include, exclude=args.exclude, cache=cache,
        use_ctags=not args.no_ctags, symbols_level=args.symbols,
    )

    # Persist/refresh the cache as JSON (same schema as --format json).
    if args.cache:
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
