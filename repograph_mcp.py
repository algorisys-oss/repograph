#!/usr/bin/env python3
"""repograph MCP server — expose repograph as MCP tools for any MCP client.

A thin, zero-dependency (stdlib-only) Model Context Protocol server over stdio.
It imports repograph in-process (no subprocess) and exposes these tools:

  - repo_index   — build/refresh the map; return the terse one-line-per-file index
  - find_symbol  — where a symbol is defined (`path:line  kind  name`)
  - search       — lexical "by intent" ranking over names + docs
  - find_refs    — lexical usages of an identifier (git grep)
  - callers      — call sites of a function/method (heuristic call graph)
  - callees      — what a function/method calls
  - impact       — transitive callers (blast radius), tests flagged
  - affected     — files/tests depending on a set of changed files

All use an incremental per-repo cache so repeat calls are fast. The call-graph
tools (callers/callees/impact/affected) build with relationship edges enabled.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (the MCP stdio
convention). No third-party packages — runs anywhere `python3` does.

Register it with any MCP client by pointing the command at:
    python3 /path/to/repograph_mcp.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Import the repograph library that sits next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import repograph as rg  # noqa: E402

SERVER_NAME = "repograph"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2024-11-05"
CACHE_DIR = Path.home() / ".claude" / "repograph-cache"


# --------------------------------------------------------------------------- #
# Core: build (incrementally) and query a repo.
# --------------------------------------------------------------------------- #

def _cache_path(repo: Path, edges: bool = False) -> Path:
    slug = str(repo.resolve()).replace(os.sep, "_").lstrip("_")
    # Edge-enabled builds use a distinct cache so they don't thrash the
    # edge-free index cache (the two carry different build_key profiles).
    suffix = ".edges.json" if edges else ".json"
    return CACHE_DIR / f"{slug}{suffix}"


def _build(path, include, exclude, symbols, no_ctags, rebuild,
           tree_sitter=False, edges=False):
    repo_dir = Path(path or os.getcwd()).expanduser()
    if not repo_dir.is_dir():
        raise ValueError(f"not a directory: {repo_dir}")
    cache_file = _cache_path(repo_dir, edges)
    cache = {}
    if cache_file.exists() and not rebuild:
        cache = rg.load_cache(cache_file)
    repo = rg.build_repo(
        repo_dir, include=include or None, exclude=exclude or None, cache=cache,
        use_ctags=not no_ctags, symbols_level=symbols or "defs",
        use_tree_sitter=tree_sitter, edges=edges,
    )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(rg.render_json(repo), encoding="utf-8")
    return repo


def tool_repo_index(args) -> str:
    repo = _build(
        args.get("path"), args.get("include"), args.get("exclude"),
        args.get("symbols", "defs"), bool(args.get("no_ctags")),
        bool(args.get("rebuild")), tree_sitter=bool(args.get("tree_sitter")),
        edges=bool(args.get("edges")),
    )
    stats = (f"{len(repo.files)} files, {repo.analyzed} analyzed, "
             f"{repo.reused} reused this run")
    return f"# {stats}\n\n" + rg.render_index(repo)


def _build_for_query(args, edges=False):
    return _build(
        args.get("path"), args.get("include"), args.get("exclude"),
        args.get("symbols", "defs"), bool(args.get("no_ctags")), False,
        tree_sitter=bool(args.get("tree_sitter")), edges=edges,
    )


def tool_find_symbol(args) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("'query' is required")
    repo = _build_for_query(args)
    rows = rg.find_symbol(repo, query, kind=args.get("kind"),
                          limit=int(args.get("limit", 50)))
    return rg.format_symbol_rows(rows)


def tool_search(args) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("'query' is required")
    repo = _build_for_query(args)
    rows = rg.search_symbols(repo, query, limit=int(args.get("limit", 30)))
    return rg.format_search_rows(rows)


def tool_find_refs(args) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("'name' is required")
    repo = _build_for_query(args)
    rows = rg.find_refs(repo.root, name, limit=int(args.get("limit", 80)),
                        definitions=rg.def_locations(repo))
    return rg.format_ref_rows(rows)


def _min_conf(args) -> str:
    """Resolve a confidence floor from args: 'strict' wins, else min_confidence."""
    if args.get("strict"):
        return "high"
    mc = args.get("min_confidence")
    return mc if mc in ("low", "medium", "high") else "low"


def tool_callers(args) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("'name' is required")
    repo = _build_for_query(args, edges=True)
    return rg.format_caller_rows(
        rg.find_callers(repo, name, limit=int(args.get("limit", 100)),
                        min_conf=_min_conf(args)))


def tool_callees(args) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("'name' is required")
    repo = _build_for_query(args, edges=True)
    return rg.format_callee_rows(
        rg.find_callees(repo, name, limit=int(args.get("limit", 100)),
                        min_conf=_min_conf(args)))


def tool_impact(args) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("'name' is required")
    repo = _build_for_query(args, edges=True)
    return rg.format_impact_rows(
        rg.find_impact(repo, name, max_depth=int(args.get("max_depth", 3)),
                       min_conf=_min_conf(args)))


def tool_affected(args) -> str:
    repo = _build_for_query(args, edges=True)
    files = args.get("files")
    if isinstance(files, str):
        changed = [c.strip() for c in files.split(",") if c.strip()]
    elif isinstance(files, list):
        changed = [str(c) for c in files]
    else:
        changed = rg.git_changed_files(repo.root)
    return rg.format_affected_rows(rg.find_affected(repo, changed))


TOOLS = [
    {
        "name": "repo_index",
        "description": (
            "Build/refresh a compact repo map and return the terse index "
            "(path | lang | lines | symbols, methods grouped as Owner{m:line}). "
            "Read this to route to a file, then open it at the listed line. "
            "Far cheaper than grepping/reading broadly. Incremental + cached."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo dir (default: cwd)"},
                "include": {"type": "array", "items": {"type": "string"},
                            "description": "glob(s) to include, e.g. 'src/*'"},
                "exclude": {"type": "array", "items": {"type": "string"},
                            "description": "glob(s) to drop, e.g. '*test*'"},
                "symbols": {"type": "string", "enum": ["none", "defs", "full"],
                            "description": "symbol detail (default defs)"},
                "no_ctags": {"type": "boolean",
                             "description": "force regex backend"},
                "rebuild": {"type": "boolean",
                            "description": "ignore cache, full re-analysis"},
            },
        },
    },
    {
        "name": "find_symbol",
        "description": (
            "Find where a function/class/type/method is defined. Returns "
            "`path:line  kind  name` rows (exact matches first) so you can open "
            "the exact spot. Names may be qualified as Owner.method."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "symbol name or substring to find"},
                "path": {"type": "string", "description": "Repo dir (default: cwd)"},
                "kind": {"type": "string",
                         "description": "optional kind filter, e.g. function, "
                                        "method, class, struct, type"},
                "limit": {"type": "integer", "description": "max rows (default 50)"},
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "no_ctags": {"type": "boolean"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search",
        "description": (
            "Lexical 'by intent' symbol search: ranks symbols by word-token "
            "overlap of the query with symbol names AND file doc comments "
            "(e.g. 'retry request' finds retryRequest / a fn documented 'retries "
            "on 5xx'). Returns `path:line kind name (score)`. NOT semantic — it "
            "matches words in names/docs, not meaning; fall back to grep for "
            "concepts not named anywhere."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "intent words, e.g. 'parse websocket message'"},
                "path": {"type": "string", "description": "Repo dir (default: cwd)"},
                "limit": {"type": "integer", "description": "max rows (default 30)"},
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "no_ctags": {"type": "boolean"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_refs",
        "description": (
            "Find where an identifier is USED (lexical, word-boundary; git grep "
            "under the hood). Returns `path:line  [def]  text`, with the "
            "definition site flagged. Approximate — it's name-based, so it can't "
            "tell a.connect() from b.connect() and may include comments/strings. "
            "Use find_symbol for definitions; use this for 'who references X'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "identifier to find usages of"},
                "path": {"type": "string", "description": "Repo dir (default: cwd)"},
                "limit": {"type": "integer", "description": "max rows (default 80)"},
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "no_ctags": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "callers",
        "description": (
            "Find call sites of a function/method NAME — 'who calls this'. "
            "Returns `path:line  in <enclosing symbol>` rows, each tagged with a "
            "confidence (high = resolved via self/super/import/local scope; "
            "medium/low = name fallback). Set strict=true or min_confidence to "
            "keep only confident edges; precise with tree_sitter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "function/method called"},
                "path": {"type": "string", "description": "Repo dir (default: cwd)"},
                "limit": {"type": "integer", "description": "max rows (default 100)"},
                "min_confidence": {"type": "string",
                                   "enum": ["low", "medium", "high"],
                                   "description": "drop edges below this (default low)"},
                "strict": {"type": "boolean",
                           "description": "only high-confidence edges (= min_confidence high)"},
                "tree_sitter": {"type": "boolean",
                                "description": "use tree-sitter for precise edges"},
                "no_ctags": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "callees",
        "description": (
            "List the symbols called from within NAME's body — 'what does this "
            "call'. Returns `callee -> def path:line` with a confidence tag, "
            "marking external callees. strict/min_confidence filter; precise "
            "with tree_sitter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "function/method to inspect"},
                "path": {"type": "string", "description": "Repo dir (default: cwd)"},
                "limit": {"type": "integer", "description": "max rows (default 100)"},
                "min_confidence": {"type": "string",
                                   "enum": ["low", "medium", "high"]},
                "strict": {"type": "boolean"},
                "tree_sitter": {"type": "boolean"},
                "no_ctags": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "impact",
        "description": (
            "Blast radius of changing NAME: its transitive callers (who breaks if "
            "you change it) over the resolved call graph, with affected test files "
            "called out. strict/min_confidence narrow it to confident edges; "
            "precise with tree_sitter. Confirm before acting on it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "symbol to assess"},
                "path": {"type": "string", "description": "Repo dir (default: cwd)"},
                "max_depth": {"type": "integer",
                              "description": "caller-graph depth (default 3)"},
                "min_confidence": {"type": "string",
                                   "enum": ["low", "medium", "high"]},
                "strict": {"type": "boolean"},
                "tree_sitter": {"type": "boolean"},
                "no_ctags": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "affected",
        "description": (
            "Given changed files, list the files (tests first) that depend on "
            "them — the test set to run after an edit. Resolves dependencies from "
            "imports + the call graph. Omit 'files' to use git's changed files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {"description": "changed paths: array or comma-separated "
                          "string; omit to use git status"},
                "path": {"type": "string", "description": "Repo dir (default: cwd)"},
                "tree_sitter": {"type": "boolean"},
                "no_ctags": {"type": "boolean"},
            },
        },
    },
]

DISPATCH = {
    "repo_index": tool_repo_index,
    "find_symbol": tool_find_symbol,
    "search": tool_search,
    "find_refs": tool_find_refs,
    "callers": tool_callers,
    "callees": tool_callees,
    "impact": tool_impact,
    "affected": tool_affected,
}


# --------------------------------------------------------------------------- #
# Minimal MCP / JSON-RPC 2.0 over stdio (newline-delimited).
# --------------------------------------------------------------------------- #

def _send(msg) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(rid, result) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code, message) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _handle(req) -> None:
    method = req.get("method")
    rid = req.get("id")
    is_notification = "id" not in req

    if method == "initialize":
        proto = (req.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        _result(rid, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        return

    if method in ("notifications/initialized", "initialized"):
        return  # notification, no reply

    if method == "ping":
        if not is_notification:
            _result(rid, {})
        return

    if method == "tools/list":
        _result(rid, {"tools": TOOLS})
        return

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if fn is None:
            _error(rid, -32602, f"unknown tool: {name}")
            return
        try:
            text = fn(args)
            _result(rid, {"content": [{"type": "text", "text": text}],
                          "isError": False})
        except Exception as exc:  # tool errors → result with isError, not protocol error
            _result(rid, {"content": [{"type": "text", "text": f"error: {exc}"}],
                          "isError": True})
        return

    if not is_notification:
        _error(rid, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue  # skip malformed frame
        try:
            _handle(req)
        except Exception as exc:  # never let one bad request kill the server
            rid = req.get("id") if isinstance(req, dict) else None
            if rid is not None:
                _error(rid, -32603, f"internal error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
