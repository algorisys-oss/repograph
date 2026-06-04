#!/usr/bin/env python3
"""token-bench.py — measure repograph's symbol-lookup token savings on a repo.

This is an *honest, static* proxy for the token win: when an agent wants to
locate a symbol, how many tokens must it read?

  - baseline (grep):  `git grep -n -w <name>` output — every match (definition
    AND usages), which the agent then has to sift through.
  - repograph (find_symbol):  just the definition row(s), `path:line kind name`.

It does NOT simulate a full agent session (reading files, reasoning) — that's
the same with or without repograph. It isolates the *discovery* step, which is
where repograph replaces grep round-trips. For an end-to-end number, run real
tasks through your agent with and without the MCP server and compare the token
counts your agent reports.

Caveats this metric is honest about:
  - It favors "where is X DEFINED" (find_symbol's job). For "where is X USED",
    repograph doesn't help — grep is still needed (no usage/call-graph data).
  - On tiny repos grep is already cheap; the win compounds with repo size and
    the number of lookups in a session.

Usage:
  python3 tools/token-bench.py [REPO_DIR] [--queries name1,name2,...] [-n N]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import repograph as rg  # noqa: E402


def tok(s: str) -> int:
    return max(1, len(s) // 4)  # rough ~4 chars/token


def find_symbol(syms, query):
    q = query.lower()
    rows = [r for r in syms if q in r[0].lower()]
    rows.sort(key=lambda r: (r[0].lower() != q, r[2], r[3]))
    return "\n".join(f"{r[2]}:{r[3]}  {r[1]}  {r[0]}" for r in rows)


def git_grep(repo: Path, query: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), "grep", "-n", "-w", query],
            capture_output=True, text=True,
        ).stdout
    except OSError:
        return ""


def auto_queries(syms, n):
    """Pick a spread of distinct, non-trivial identifiers to look up."""
    seen, out = set(), []
    for name, _kind, _path, _line in syms:
        base = name.split(".")[-1]            # bare identifier for grep
        if len(base) < 4 or base in seen:
            continue
        seen.add(base)
        out.append(base)
    # spread across the list rather than taking the first N alphabetically
    if len(out) <= n:
        return out
    step = len(out) / n
    return [out[int(i * step)] for i in range(n)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path, nargs="?", default=Path("."))
    ap.add_argument("--queries", help="comma-separated identifiers (default: auto)")
    ap.add_argument("-n", type=int, default=12, help="how many auto queries (default 12)")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    g = rg.build_repo(repo)
    syms = [(s.name, s.kind, f.rel_path, s.line) for f in g.files for s in f.symbols]
    if not syms:
        print("No symbols extracted — nothing to benchmark.", file=sys.stderr)
        return 1

    queries = (args.queries.split(",") if args.queries
               else auto_queries(syms, args.n))

    print(f"repo: {repo}")
    print(f"files: {len(g.files)}   symbols: {len(syms)}   "
          f"index tokens: ~{tok(rg.render_index(g)) // 1000}k\n")

    print(f"{'query':<22}{'grep lines':>11}{'grep tok':>10}{'find tok':>10}   ratio")
    gt = ft = 0
    for q in queries:
        go, fo = git_grep(repo, q), find_symbol(syms, q)
        gtk, ftk = tok(go), tok(fo)
        gt += gtk
        ft += ftk
        ratio = gtk / ftk if ftk else 0
        print(f"{q:<22}{go.count(chr(10)):>11}{gtk:>10}{ftk:>10}{ratio:>7.1f}x")
    print("-" * 62)
    avg = gt / ft if ft else 0
    print(f"{'TOTAL (' + str(len(queries)) + ' lookups)':<22}"
          f"{'':>11}{gt:>10}{ft:>10}{avg:>7.1f}x")
    print(f"\nDiscovery cost: grep ~{gt} tok vs find_symbol ~{ft} tok "
          f"=> ~{avg:.0f}x fewer tokens to locate symbols.")
    print("NB: discovery step only; not a full-session A/B. See file header.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
