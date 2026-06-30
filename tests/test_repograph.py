"""Tests for repograph against a temp fixture repo.

Stdlib unittest only (no pytest needed). Run with:
    python -m unittest discover -s repograph/tests
or:
    python repograph/tests/test_repograph.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# Make the parent package importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repograph as rg  # noqa: E402


# A small fixture repo spread across a few languages, plus noise to ignore.
FIXTURE = {
    "README.md": "# Demo\n\nA tiny fixture repo used by the tests.\n\nSecond paragraph ignored.\n",
    "src/app.py": (
        '"""App entry point."""\n'
        "import os\n"
        "from utils import helper\n"
        "\n"
        "\n"
        "def main():\n"
        "    return helper()\n"
        "\n"
        "\n"
        "class Server:\n"
        "    pass\n"
    ),
    "src/util.zig": (
        "// A small helper module.\n"
        'const std = @import("std");\n'
        "\n"
        "pub fn add(a: i32, b: i32) i32 {\n"
        "    return a + b;\n"
        "}\n"
        "\n"
        "pub const Point = struct {\n"
        "    x: i32,\n"
        "};\n"
    ),
    "lib/widget.js": (
        "// widget helpers\n"
        "import { thing } from './thing';\n"
        "export function build() {}\n"
        "export class Widget {}\n"
    ),
    "lib/Widget.tsx": (
        "import { Component } from 'solid-js';\n"
        "export const Widget: Component<Props> = (props) => {\n"
        "  return null;\n"
        "};\n"
        "export enum Mode { On, Off }\n"
        "export const load = async (id: string): Promise<Data> => fetch(id);\n"
    ),
    "lib/store.ex": (
        "defmodule Store do\n"
        "  alias Store.Repo\n"
        "  def get(id) do\n"
        "    Repo.fetch(id)\n"
        "  end\n"
        "  defp normalize(x), do: x\n"
        "end\n"
    ),
    # Noise that must be skipped:
    "node_modules/dep/index.js": "function shouldBeSkipped() {}\n",
    "__pycache__/app.cpython.pyc": "function alsoSkipped() {}\n",
}


def write_fixture(root: Path):
    for rel, content in FIXTURE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


class RepoGraphTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_fixture(self.root)
        # Pin the regex backend so these assertions are deterministic whether or
        # not the dev machine has universal-ctags installed (ctags is covered by
        # the dedicated CtagsTest below).
        self.repo = rg.build_repo(self.root, use_ctags=False)
        self.by_path = {f.rel_path: f for f in self.repo.files}

    def tearDown(self):
        self._tmp.cleanup()

    def test_noise_dirs_skipped(self):
        paths = set(self.by_path)
        self.assertNotIn("node_modules/dep/index.js", paths)
        self.assertNotIn("__pycache__/app.cpython.pyc", paths)

    def test_real_files_present(self):
        for rel in ("src/app.py", "src/util.zig", "lib/widget.js", "README.md"):
            self.assertIn(rel, self.by_path, f"{rel} missing")

    def test_language_detection(self):
        self.assertEqual(self.by_path["src/app.py"].language, "python")
        self.assertEqual(self.by_path["src/util.zig"].language, "zig")
        self.assertEqual(self.by_path["lib/widget.js"].language, "javascript")
        self.assertEqual(self.by_path["README.md"].language, "")

    def test_python_symbols_and_lines(self):
        syms = {s.name: s for s in self.by_path["src/app.py"].symbols}
        self.assertIn("main", syms)
        self.assertIn("Server", syms)
        # main() is on line 6, Server on line 10 (1-based) in the fixture above.
        self.assertEqual(syms["main"].line, 6)
        self.assertEqual(syms["main"].kind, "function")
        self.assertEqual(syms["Server"].line, 10)
        self.assertEqual(syms["Server"].kind, "class")

    def test_zig_symbols(self):
        syms = {s.name: s.kind for s in self.by_path["src/util.zig"].symbols}
        self.assertEqual(syms.get("add"), "function")
        self.assertEqual(syms.get("Point"), "type")

    def test_js_symbols(self):
        syms = {s.name: s.kind for s in self.by_path["lib/widget.js"].symbols}
        self.assertEqual(syms.get("build"), "function")
        self.assertEqual(syms.get("Widget"), "class")

    def test_imports(self):
        self.assertEqual(self.by_path["src/app.py"].imports, ["os", "utils"])
        self.assertEqual(self.by_path["src/util.zig"].imports, ["std"])
        self.assertEqual(self.by_path["lib/widget.js"].imports, ["./thing"])

    def test_typescript_typed_arrows_and_enum(self):
        f = self.by_path["lib/Widget.tsx"]
        self.assertEqual(f.language, "typescript")
        syms = {s.name: (s.kind, s.line) for s in f.symbols}
        # Typed arrow component (the pattern plain regex used to miss).
        self.assertEqual(syms.get("Widget"), ("function", 2))
        self.assertEqual(syms.get("Mode"), ("enum", 5))
        # Typed async arrow with a return-type annotation.
        self.assertEqual(syms.get("load"), ("function", 6))

    def test_elixir(self):
        f = self.by_path["lib/store.ex"]
        self.assertEqual(f.language, "elixir")
        syms = {s.name: (s.kind, s.line) for s in f.symbols}
        self.assertEqual(syms.get("Store"), ("module", 1))
        self.assertEqual(syms.get("get"), ("function", 3))
        self.assertEqual(syms.get("normalize"), ("function", 6))
        self.assertEqual(f.imports, ["Store.Repo"])

    def test_leading_doc(self):
        self.assertEqual(self.by_path["src/app.py"].doc, "App entry point.")
        self.assertEqual(self.by_path["src/util.zig"].doc, "A small helper module.")

    def test_readme_summary(self):
        self.assertEqual(self.repo.readme_summary, "Demo A tiny fixture repo used by the tests.")

    def test_include_exclude_filters(self):
        only_src = rg.build_repo(self.root, include=["src/*"])
        paths = {f.rel_path for f in only_src.files}
        self.assertIn("src/app.py", paths)
        self.assertNotIn("lib/widget.js", paths)
        self.assertNotIn("README.md", paths)

        no_js = rg.build_repo(self.root, exclude=["*.js"])
        paths = {f.rel_path for f in no_js.files}
        self.assertNotIn("lib/widget.js", paths)
        self.assertIn("src/app.py", paths)

    def test_markdown_has_links_with_correct_lines(self):
        md = rg.render_markdown(self.repo)
        # Symbol link points at the exact source line.
        self.assertIn("[main](src/app.py#L6)", md)
        self.assertIn("[Point](src/util.zig#L8)", md)
        self.assertIn("## Overview", md)
        self.assertIn("## Tree", md)
        # Heuristic disclaimer is present.
        self.assertIn("heuristic", md.lower())
        # Compact: the redundant trailing "— path:line" is gone.
        self.assertNotIn("— src/app.py:6", md)

    def test_index_format(self):
        idx = rg.render_index(self.repo)
        # One terse line per file with name:line pairs (symbols only, no imports).
        self.assertIn("src/app.py | python | 11 | main:6 Server:10", idx)
        self.assertIn("src/util.zig | zig | 10 | add:4 Point:8", idx)

    def test_index_is_smaller_than_markdown(self):
        self.assertLess(len(rg.render_index(self.repo)), len(rg.render_markdown(self.repo)))

    def test_json_roundtrip(self):
        d = rg.repo_to_dict(self.repo)
        files = rg.files_from_dict(d)
        self.assertIn("src/app.py", files)
        app = files["src/app.py"]
        names = {s.name: s.line for s in app.symbols}
        self.assertEqual(names["main"], 6)
        self.assertTrue(app.content_hash)  # hash is populated

    def test_incremental_reuses_unchanged_and_reanalyzes_changed(self):
        # Use the same backend as setUp (use_ctags=False) so the cached nodes'
        # build profile matches and reuse is exercised (see test_cache_* below
        # for the profile-change invalidation path).
        cache = {f.rel_path: f for f in self.repo.files}
        # Re-run with the full cache, nothing changed → everything reused.
        again = rg.build_repo(self.root, cache=cache, use_ctags=False)
        self.assertEqual(again.analyzed, 0)
        self.assertEqual(again.reused, len(self.repo.files))
        self.assertEqual(again.dropped, 0)

        # Change one file, delete another, then re-run with the old cache.
        (self.root / "src" / "app.py").write_text(
            "def main():\n    pass\n\n\ndef extra():\n    pass\n", encoding="utf-8"
        )
        (self.root / "lib" / "widget.js").unlink()
        cache = {f.rel_path: f for f in again.files}
        third = rg.build_repo(self.root, cache=cache, use_ctags=False)
        by_path = {f.rel_path: f for f in third.files}
        self.assertEqual(third.analyzed, 1)            # only app.py re-analyzed
        self.assertEqual(third.dropped, 1)             # widget.js gone
        self.assertNotIn("lib/widget.js", by_path)
        new_syms = {s.name for s in by_path["src/app.py"].symbols}
        self.assertEqual(new_syms, {"main", "extra"})  # reflects new content


class CtagsTest(unittest.TestCase):
    """The universal-ctags fast-path, exercised via stubs (no binary needed)."""

    def setUp(self):
        # Always restore the detection memo so tests don't leak state.
        self.addCleanup(setattr, rg, "_CTAGS_OK", None)
        rg._CTAGS_OK = None

    def _fake_run(self, stdout):
        """A subprocess.run stub returning canned stdout."""
        class _Proc:
            pass
        def run(*a, **k):
            p = _Proc()
            p.stdout = stdout
            p.returncode = 0
            return p
        return run

    def test_ctags_version_gate(self):
        """Universal Ctags is accepted; Exuberant Ctags is rejected."""
        with unittest.mock.patch.object(
            rg.subprocess, "run", self._fake_run("Universal Ctags 6.0.0\n")
        ):
            rg._CTAGS_OK = None
            self.assertTrue(rg.ctags_available())
        with unittest.mock.patch.object(
            rg.subprocess, "run", self._fake_run("Exuberant Ctags 5.9~svn\n")
        ):
            rg._CTAGS_OK = None
            self.assertFalse(rg.ctags_available())

    def test_ctags_ndjson_parsing(self):
        """NDJSON → Symbols: skip ptag/junk, map kinds, qualify, filter members."""
        import json
        lines = [
            json.dumps({"_type": "ptag", "name": "!_TAG_FILE_FORMAT"}),
            "not json — must be skipped",
            json.dumps({"_type": "tag", "name": "Server", "path": "/x/a.ts",
                        "line": 1, "kind": "class"}),
            json.dumps({"_type": "tag", "name": "get", "path": "/x/a.ts",
                        "line": 8, "kind": "method", "scope": "Server"}),
            json.dumps({"_type": "tag", "name": "put", "path": "/x/a.ts",
                        "line": 12, "kind": "method", "scope": "pkg::Server"}),
            json.dumps({"_type": "tag", "name": "_field", "path": "/x/a.ts",
                        "line": 15, "kind": "member", "scope": "Server"}),
        ]
        stdout = "\n".join(lines) + "\n"

        with unittest.mock.patch.object(rg.subprocess, "run", self._fake_run(stdout)):
            defs = rg.run_ctags(["/x/a.ts"], "defs")
        syms = {(s.name, s.kind, s.line) for s in defs["/x/a.ts"]}
        self.assertIn(("Server", "class", 1), syms)
        self.assertIn(("Server.get", "method", 8), syms)   # scope-qualified
        self.assertIn(("Server.put", "method", 12), syms)  # pkg::Server → Server
        self.assertNotIn(("_field", "member", 15),
                         {(s.name, s.kind, s.line) for s in defs["/x/a.ts"]})
        # Members survive at "full".
        with unittest.mock.patch.object(rg.subprocess, "run", self._fake_run(stdout)):
            full = rg.run_ctags(["/x/a.ts"], "full")
        self.assertTrue(any(s.name == "_field" for s in full["/x/a.ts"]))

    def test_ctags_member_promotion_by_scopekind(self):
        """A 'member' in a class (Python-style) becomes a qualified method;
        a 'member' in a struct (C-style data field) stays a field."""
        import json
        stdout = "\n".join([
            json.dumps({"_type": "tag", "name": "C", "path": "/p", "line": 1,
                        "kind": "class"}),
            json.dumps({"_type": "tag", "name": "m", "path": "/p", "line": 4,
                        "kind": "member", "scope": "C", "scopeKind": "class"}),
            json.dumps({"_type": "tag", "name": "x", "path": "/p", "line": 8,
                        "kind": "member", "scope": "Pt", "scopeKind": "struct"}),
        ]) + "\n"
        with unittest.mock.patch.object(rg.subprocess, "run", self._fake_run(stdout)):
            defs = rg.run_ctags(["/p"], "defs")["/p"]
        d = {(s.name, s.kind) for s in defs}
        self.assertIn(("C.m", "method"), d)        # class member → qualified method
        self.assertNotIn(("x", "member"), d)       # struct field dropped at defs
        self.assertFalse(any(s.name == "x" for s in defs))
        with unittest.mock.patch.object(rg.subprocess, "run", self._fake_run(stdout)):
            full = rg.run_ctags(["/p"], "full")["/p"]
        self.assertTrue(any(s.name == "x" and s.kind == "member" for s in full))
        # The struct field is never promoted to a method, even at full.
        self.assertFalse(any(s.name == "Pt.x" for s in full))

    def test_run_ctags_dedupes_and_sorts(self):
        import json
        stdout = "\n".join([
            json.dumps({"_type": "tag", "name": "b", "path": "/p", "line": 9,
                        "kind": "function"}),
            json.dumps({"_type": "tag", "name": "a", "path": "/p", "line": 5,
                        "kind": "function"}),
            json.dumps({"_type": "tag", "name": "a", "path": "/p", "line": 2,
                        "kind": "function"}),  # earlier dup of a → wins
        ]) + "\n"
        with unittest.mock.patch.object(rg.subprocess, "run", self._fake_run(stdout)):
            out = rg.run_ctags(["/p"], "defs")["/p"]
        self.assertEqual([(s.name, s.line) for s in out], [("a", 2), ("b", 9)])

    def test_ctags_path_via_build_repo(self):
        """build_repo uses injected ctags symbols when ctags is 'available'."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "a.py").write_text("class C:\n    def m(self):\n        pass\n")

        def fake_run_ctags(paths, level):
            return {p: [rg.Symbol("C.m", "method", 2)] for p in paths}

        with unittest.mock.patch.object(rg, "ctags_available", lambda: True), \
             unittest.mock.patch.object(rg, "run_ctags", fake_run_ctags):
            repo = rg.build_repo(root, use_ctags=True)
        app = {f.rel_path: f for f in repo.files}["a.py"]
        self.assertEqual([(s.name, s.kind, s.line) for s in app.symbols],
                         [("C.m", "method", 2)])

    def test_ctags_empty_fallback_to_regex(self):
        """A known-language file ctags returns nothing for falls back to regex."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "a.py").write_text("def top():\n    pass\n")

        with unittest.mock.patch.object(rg, "ctags_available", lambda: True), \
             unittest.mock.patch.object(rg, "run_ctags", lambda paths, level: {}):
            repo = rg.build_repo(root, use_ctags=True)
        app = {f.rel_path: f for f in repo.files}["a.py"]
        self.assertEqual([s.name for s in app.symbols], ["top"])  # regex result

    def test_symbols_level_none(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "a.py").write_text("def top():\n    pass\n")
        repo = rg.build_repo(root, use_ctags=False, symbols_level="none")
        app = {f.rel_path: f for f in repo.files}["a.py"]
        self.assertEqual(app.symbols, [])

    def test_index_grouped_rendering(self):
        """Methods collapse under their owner; the class line is preserved."""
        repo = rg.Repo(root=Path("/x"), name="x")
        repo.files = [rg.FileNode(
            rel_path="a.ts", language="typescript", line_count=30,
            symbols=[
                rg.Symbol("Server", "class", 1),
                rg.Symbol("main", "function", 6),
                rg.Symbol("Server.get", "method", 8),
                rg.Symbol("Server.put", "method", 12),
            ],
        )]
        idx = rg.render_index(repo)
        self.assertIn("a.ts | typescript | 30 | Server:1{get:8 put:12} main:6", idx)

    def test_index_groups_ownerless_methods(self):
        """Methods whose owner isn't a captured symbol still group at first use."""
        repo = rg.Repo(root=Path("/x"), name="x")
        repo.files = [rg.FileNode(
            rel_path="a.cpp", language="c", line_count=40,
            symbols=[rg.Symbol("Foo.a", "method", 3), rg.Symbol("Foo.b", "method", 9)],
        )]
        idx = rg.render_index(repo)
        self.assertIn("a.cpp | c | 40 | Foo{a:3 b:9}", idx)

    def test_symbol_cap_overflow(self):
        repo = rg.Repo(root=Path("/x"), name="x")
        n = rg.MAX_SYMBOLS_PER_FILE + 5
        repo.files = [rg.FileNode(
            rel_path="big.py", language="python", line_count=999,
            symbols=[rg.Symbol(f"f{i}", "function", i + 1) for i in range(n)],
        )]
        idx = rg.render_index(repo)
        self.assertIn("+5 more", idx)
        self.assertIn("f0:1", idx)                                   # kept
        self.assertNotIn(f"f{n - 1}:{n}", idx)                       # dropped

    def test_cache_compat_qualified_names(self):
        """A folded qualified name round-trips through the JSON cache schema."""
        repo = rg.Repo(root=Path("/x"), name="x")
        repo.files = [rg.FileNode(
            rel_path="a.py", language="python", line_count=3, content_hash="h",
            symbols=[rg.Symbol("C.m", "method", 2)],
        )]
        files = rg.files_from_dict(rg.repo_to_dict(repo))
        self.assertEqual(files["a.py"].symbols[0].name, "C.m")
        self.assertEqual(files["a.py"].symbols[0].kind, "method")


class QueryTest(unittest.TestCase):
    """find_symbol / search_symbols / find_refs / tokenize."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_fixture(self.root)
        self.repo = rg.build_repo(self.root, use_ctags=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_tokenize(self):
        self.assertEqual(rg.tokenize("retryRequest"), ["retry", "request"])
        self.assertEqual(rg.tokenize("with_retries"), ["with", "retries"])
        self.assertEqual(rg.tokenize("HTTPServer"), ["http", "server"])
        self.assertEqual(rg.tokenize("Owner.doThing"), ["owner", "do", "thing"])

    def test_find_symbol(self):
        rows = rg.find_symbol(self.repo, "main")
        self.assertIn(("src/app.py", 6, "function", "main"), rows)
        # substring + exact ordering: exact 'Widget' matches come first
        names = [r[3] for r in rg.find_symbol(self.repo, "Widget")]
        self.assertIn("Widget", names)

    def test_find_symbol_kind_filter(self):
        rows = rg.find_symbol(self.repo, "Server", kind="class")
        self.assertTrue(all(r[2] == "class" for r in rows))
        self.assertTrue(any(r[3] == "Server" for r in rows))

    def test_search_matches_name_tokens(self):
        rows = rg.search_symbols(self.repo, "server")
        self.assertTrue(rows and rows[0][3] == "Server")

    def test_search_matches_doc_tokens(self):
        # src/app.py docstring is "App entry point." — a doc-word query should
        # surface symbols from that file even though names don't contain them.
        hits = {(r[0], r[3]) for r in rg.search_symbols(self.repo, "entry point")}
        self.assertIn(("src/app.py", "main"), hits)

    def test_find_refs_stdlib_scan(self):
        # Non-git temp dir → stdlib scan path. 'helper' is imported and called.
        defs = rg.def_locations(self.repo)
        rows = rg.find_refs(self.root, "helper", definitions=defs)
        hits = {(r[0], r[1]) for r in rows}
        self.assertIn(("src/app.py", 3), hits)   # `from utils import helper`
        self.assertIn(("src/app.py", 7), hits)   # `return helper()`

    def test_format_helpers(self):
        self.assertEqual(rg.format_symbol_rows([]), "(no matches)")
        self.assertEqual(
            rg.format_ref_rows([("a.py", 3, True, "def x():")]),
            "a.py:3  [def] def x():",
        )


@unittest.skipIf(shutil.which("git") is None, "git not available")
class GitFastPathTest(unittest.TestCase):
    """The git-blob-sha fast path reuses unchanged tracked files without reading."""

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True,
                       capture_output=True)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "x.py").write_text("def a():\n    pass\n")
        (self.root / "y.py").write_text("def b():\n    pass\n")
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")

    def tearDown(self):
        self._tmp.cleanup()

    def _cache_of(self, repo):
        return {f.rel_path: f for f in repo.files}

    def test_warm_reuse_and_gitsha_stored(self):
        first = rg.build_repo(self.root, use_ctags=False)
        # gitsha learned for every tracked file on the cold pass
        self.assertTrue(all(f.gitsha for f in first.files))
        again = rg.build_repo(self.root, cache=self._cache_of(first), use_ctags=False)
        self.assertEqual(again.analyzed, 0)
        self.assertEqual(again.reused, 2)

    def test_committed_change_reanalyzes_only_that_file(self):
        first = rg.build_repo(self.root, use_ctags=False)
        (self.root / "y.py").write_text("def b():\n    pass\ndef c():\n    pass\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "upd")
        again = rg.build_repo(self.root, cache=self._cache_of(first), use_ctags=False)
        self.assertEqual(again.analyzed, 1)
        self.assertEqual(again.reused, 1)

    def test_unstaged_edit_is_reanalyzed(self):
        first = rg.build_repo(self.root, use_ctags=False)
        # modify without committing → blob sha unchanged, but working tree dirty
        (self.root / "x.py").write_text("def a():\n    pass\ndef z():\n    pass\n")
        again = rg.build_repo(self.root, cache=self._cache_of(first), use_ctags=False)
        self.assertEqual(again.analyzed, 1)
        by = {f.rel_path: f for f in again.files}
        self.assertEqual({s.name for s in by["x.py"].symbols}, {"a", "z"})

    def test_untracked_included_ignored_and_artifacts_excluded(self):
        (self.root / "new.py").write_text("def n():\n    pass\n")     # untracked
        (self.root / "ign.py").write_text("def g():\n    pass\n")     # ignored
        (self.root / ".gitignore").write_text("ign.py\n")
        (self.root / ".repograph").mkdir()
        (self.root / ".repograph" / "map.index").write_text("x | - | 1 |\n")
        paths = {f.rel_path for f in rg.build_repo(self.root, use_ctags=False).files}
        self.assertIn("x.py", paths)                 # tracked
        self.assertIn("new.py", paths)               # untracked, not ignored
        self.assertNotIn("ign.py", paths)            # gitignored → excluded
        self.assertNotIn(".repograph/map.index", paths)  # own artifact → excluded

    def test_rename_marks_new_path_dirty(self):
        # `git status --porcelain -z` emits a rename as "R  <new>\0<old>\0";
        # git_dirty_files must take the NEW (working-tree) path, not the old.
        self._git("mv", "x.py", "renamed.py")
        dirty = rg.git_dirty_files(self.root)
        self.assertIn("renamed.py", dirty)           # new path is what exists now
        self.assertNotIn("x.py", dirty)              # old path is gone

    def test_find_refs_uses_git_grep(self):
        # In a git repo, find_refs goes through `git grep` (not the stdlib scan).
        (self.root / "x.py").write_text("def a():\n    return a()\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "ref")
        repo = rg.build_repo(self.root, use_ctags=False)
        rows = rg.find_refs(self.root, "a", definitions=rg.def_locations(repo))
        hits = {(r[0], r[1]) for r in rows}
        self.assertIn(("x.py", 1), hits)             # def a()
        self.assertIn(("x.py", 2), hits)             # return a()
        self.assertTrue(any(r[2] for r in rows if r[0] == "x.py" and r[1] == 1))  # def flagged


class ReviewFixTest(unittest.TestCase):
    """Regression tests for the reviewed findings (cache profile + artifacts)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.py").write_text(
            "def a():\n    pass\nclass C:\n    def m(self):\n        pass\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_cache_symbols_none_clears_stale_symbols(self):
        first = rg.build_repo(self.root, use_ctags=False, symbols_level="defs")
        self.assertTrue([s for f in first.files for s in f.symbols])
        cache = {f.rel_path: f for f in first.files}
        again = rg.build_repo(self.root, cache=cache, use_ctags=False,
                              symbols_level="none")
        self.assertEqual(again.analyzed, 1)     # re-analyzed, not reused stale
        self.assertEqual([s for f in again.files for s in f.symbols], [])

    def test_cache_level_change_reanalyzes(self):
        first = rg.build_repo(self.root, use_ctags=False, symbols_level="defs")
        cache = {f.rel_path: f for f in first.files}
        again = rg.build_repo(self.root, cache=cache, use_ctags=False,
                              symbols_level="full")
        self.assertEqual(again.reused, 0)       # profile changed → nothing reused

    def test_cache_without_build_key_reanalyzes(self):
        # Simulate a pre-build_key (old) cache: node present, build_key empty.
        first = rg.build_repo(self.root, use_ctags=False)
        node = first.files[0]
        node.build_key = ""
        again = rg.build_repo(self.root, cache={node.rel_path: node},
                              use_ctags=False)
        self.assertEqual(again.analyzed, 1)     # mismatch → re-analyze, no crash
        self.assertTrue([s for f in again.files for s in f.symbols])

    def test_cache_same_profile_reuses(self):
        first = rg.build_repo(self.root, use_ctags=False, symbols_level="defs")
        cache = {f.rel_path: f for f in first.files}
        again = rg.build_repo(self.root, cache=cache, use_ctags=False,
                              symbols_level="defs")
        self.assertEqual(again.reused, len(first.files))
        self.assertEqual(again.analyzed, 0)

    def test_build_key_round_trips_through_cache_json(self):
        repo = rg.build_repo(self.root, use_ctags=False)
        files = rg.files_from_dict(rg.repo_to_dict(repo))
        # build_key encodes "<level>:<backend>:<edges?>" — regex backend, no edges.
        self.assertEqual(files["a.py"].build_key, "defs:rx:0")

    def test_repograph_artifacts_not_indexed_non_git(self):
        (self.root / ".repograph").mkdir()
        (self.root / ".repograph" / "map.index").write_text("x | - | 1 |\n")
        (self.root / ".repograph" / "map.graph.json").write_text("{}\n")
        paths = {f.rel_path for f in rg.build_repo(self.root, use_ctags=False).files}
        self.assertIn("a.py", paths)
        self.assertFalse(any(p.startswith(".repograph/") for p in paths))


@unittest.skipIf(shutil.which("git") is None, "git not available")
class InitTest(unittest.TestCase):
    """`repograph --init` scaffolds the committed-map workflow into a repo."""

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True,
                       capture_output=True)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("def main():\n    pass\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "t.py").write_text("def test_x():\n    pass\n")
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")

    def tearDown(self):
        self._tmp.cleanup()

    def test_init_writes_map_hook_and_activates(self):
        rc = rg.cmd_init(self.root, include=None, exclude=None,
                         symbols_level="defs", use_ctags=False)
        self.assertEqual(rc, 0)
        # canonical artifacts (not the old map.index/map.graph.json names)
        for name in ("index.txt", "map.md", "graph.json"):
            self.assertTrue((self.root / ".repograph" / name).is_file(), name)
        hook = self.root / ".githooks" / "pre-commit"
        self.assertTrue(hook.is_file())
        self.assertTrue(os.access(hook, os.X_OK))           # executable
        body = hook.read_text()
        self.assertIn("node_modules/.bin/repograph", body)  # resolves npm bin
        self.assertIn("index.txt", body)
        self.assertIn("git add", body)                      # stages the map
        # activated for this clone
        out = subprocess.run(["git", "-C", str(self.root), "config",
                              "core.hooksPath"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), ".githooks")

    def test_init_bakes_scope_flags_into_hook(self):
        rg.cmd_init(self.root, include=["src/*"], exclude=["*test*"],
                    symbols_level="defs", use_ctags=False)
        body = (self.root / ".githooks" / "pre-commit").read_text()
        self.assertIn("--include 'src/*'", body)
        self.assertIn("--exclude '*test*'", body)
        # scope is honored in the generated index too
        index = (self.root / ".repograph" / "index.txt").read_text()
        self.assertIn("src/app.py", index)
        self.assertNotIn("tests/t.py", index)

    def test_init_non_git_errors(self):
        with tempfile.TemporaryDirectory() as d:
            rc = rg.cmd_init(Path(d), include=None, exclude=None,
                             symbols_level="defs", use_ctags=False)
            self.assertEqual(rc, 1)

    def test_scope_flags_helper(self):
        self.assertEqual(rg._scope_flags(None, None), "")
        self.assertEqual(rg._scope_flags(["a/*"], ["b"]),
                         " --include 'a/*' --exclude b")


# A fixture with a clear call graph + inheritance, for the edge/query tests.
EDGE_FIXTURE = {
    "core.py": (
        '"""Core module."""\n'
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def main():\n"
        "    return helper()\n"
        "\n"
        "class Base:\n"
        "    def run(self):\n"
        "        return helper()\n"
        "\n"
        "class Widget(Base):\n"
        "    def render(self):\n"
        "        return self.run()\n"
    ),
    "tests/test_core.py": (
        "from core import main\n"
        "def test_main():\n"
        "    assert main() == 1\n"
    ),
}


class EdgeExtractionTest(unittest.TestCase):
    """Regex relationship-edge extraction: spans, calls, extends/implements."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for rel, content in EDGE_FIXTURE.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self.repo = rg.build_repo(self.root, use_ctags=False, edges=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, rel):
        return next(f for f in self.repo.files if f.rel_path == rel)

    def test_spans_assigned(self):
        syms = {s.name: s for s in self._file("core.py").symbols}
        self.assertEqual(syms["helper"].line, 2)
        self.assertGreaterEqual(syms["helper"].end, 3)
        self.assertGreater(syms["main"].end, syms["main"].line)

    def test_extends_edge(self):
        edges = self._file("core.py").edges
        self.assertTrue(any(e.kind == "extends" and e.src == "Widget"
                            and e.dst == "Base" for e in edges))

    def test_call_edges_attributed_to_enclosing(self):
        edges = self._file("core.py").edges
        calls = {(e.src, e.dst) for e in edges if e.kind == "calls"}
        self.assertIn(("main", "helper"), calls)
        # `helper(` in a comment/keyword must not be attributed to file scope here

    def test_edges_absent_without_flag(self):
        repo = rg.build_repo(self.root, use_ctags=False)  # edges=False default
        self.assertTrue(all(not f.edges for f in repo.files))

    def test_build_key_includes_edges(self):
        files = rg.files_from_dict(rg.repo_to_dict(self.repo))
        # edge tag is versioned ("e3" = edges carry receivers + import bindings)
        self.assertEqual(files["core.py"].build_key, "defs:rx:e3")

    def test_edges_round_trip_json(self):
        files = rg.files_from_dict(rg.repo_to_dict(self.repo))
        calls = {(e.src, e.dst) for e in files["core.py"].edges if e.kind == "calls"}
        self.assertIn(("main", "helper"), calls)


class RelationshipQueryTest(unittest.TestCase):
    """callers / callees / impact / affected over the edge graph."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for rel, content in EDGE_FIXTURE.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self.repo = rg.build_repo(self.root, use_ctags=False, edges=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_callers_of_helper(self):
        callers = {r[2] for r in rg.find_callers(self.repo, "helper")}
        self.assertIn("main", callers)
        self.assertIn("run", callers)

    def test_callees_of_main(self):
        callees = {r[0] for r in rg.find_callees(self.repo, "main")}
        self.assertIn("helper", callees)

    def test_callees_resolve_to_def(self):
        rows = rg.find_callees(self.repo, "main")
        helper_rows = [r for r in rows if r[0] == "helper"]
        self.assertTrue(helper_rows and helper_rows[0][1] == "core.py")

    def test_impact_reaches_transitive_callers(self):
        rows, files, tests = rg.find_impact(self.repo, "helper")
        callers = {r[1] for r in rows}
        self.assertIn("main", callers)

    def test_affected_flags_test_file(self):
        tests, others = rg.find_affected(self.repo, ["core.py"])
        self.assertIn("tests/test_core.py", tests)

    def test_is_test_path(self):
        self.assertTrue(rg.is_test_path("tests/test_core.py"))
        self.assertTrue(rg.is_test_path("foo.spec.js"))
        self.assertFalse(rg.is_test_path("core.py"))

    def test_query_formatters_empty(self):
        self.assertIn("no callers", rg.format_caller_rows([]))
        self.assertIn("no callees", rg.format_callee_rows([]))


@unittest.skipUnless(rg.tree_sitter_available(),
                     "tree-sitter grammar pack not installed")
class TreeSitterBackendTest(unittest.TestCase):
    """The optional tree-sitter backend: precise symbols + call edges."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "core.py").write_text(EDGE_FIXTURE["core.py"], encoding="utf-8")
        self.repo = rg.build_repo(self.root, use_ctags=False,
                                  use_tree_sitter=True, edges=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_methods_qualified(self):
        names = {s.name for f in self.repo.files for s in f.symbols}
        self.assertIn("Widget.render", names)
        self.assertIn("Base.run", names)

    def test_precise_call_edges(self):
        edges = self.repo.files[0].edges
        calls = {(e.src, e.dst) for e in edges if e.kind == "calls"}
        # render calls self.run() -> attributed to the exact method, callee 'run'
        self.assertIn(("Widget.render", "run"), calls)
        self.assertIn(("main", "helper"), calls)

    def test_backend_tag_in_build_key(self):
        self.assertTrue(self.repo.files[0].build_key.startswith("defs:ts:"))

    def test_direct_ts_extract(self):
        data = EDGE_FIXTURE["core.py"].encode("utf-8")
        res = rg.ts_extract(data, "python", ".py", "defs")
        self.assertIsNotNone(res)
        syms, edges = res
        # exact end lines from the AST
        helper = next(s for s in syms if s.name == "helper")
        self.assertEqual(helper.line, 2)
        self.assertEqual(helper.end, 3)


class EdgeResolutionTest(unittest.TestCase):
    """resolve_edges + confidence on the regex backend (local + name fallback)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for rel, content in EDGE_FIXTURE.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self.repo = rg.build_repo(self.root, use_ctags=False, edges=True)
        self.resolved = rg.resolve_edges(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_local_free_call_high_confidence(self):
        # main() calls helper() — a free call to a same-file def -> high/local.
        hit = [r for r in self.resolved
               if r.src == "main" and r.dst == "helper"]
        self.assertTrue(hit)
        self.assertEqual(hit[0].conf, "high")
        self.assertEqual(hit[0].prov, "local")
        self.assertEqual(hit[0].dst_file, "core.py")

    def test_callees_carry_confidence(self):
        rows = rg.find_callees(self.repo, "main")
        # rows are (callee, dst_file, dst_line, call_line, conf)
        helper = [r for r in rows if r[0] == "helper"][0]
        self.assertEqual(helper[4], "high")

    def test_import_resolves_cross_file(self):
        # tests/test_core.py does `from core import main` then `main()` — the
        # import binding resolves the call to core.py at high confidence.
        hit = [r for r in self.resolved
               if r.dst == "main" and r.src_file == "tests/test_core.py"]
        self.assertTrue(hit)
        self.assertEqual((hit[0].dst_file, hit[0].conf, hit[0].prov),
                         ("core.py", "high", "import"))

    def test_min_confidence_filters(self):
        all_callers = rg.find_callers(self.repo, "helper", min_conf="low")
        high_callers = rg.find_callers(self.repo, "helper", min_conf="high")
        self.assertGreaterEqual(len(all_callers), len(high_callers))
        self.assertTrue(all(r[4] == "high" for r in high_callers))

    def test_resolved_edge_round_trips_recv(self):
        # receiver survives the JSON cache round-trip
        files = rg.files_from_dict(rg.repo_to_dict(self.repo))
        recvs = {e.recv for e in files["core.py"].edges if e.kind == "calls"}
        self.assertIn("self", recvs)  # self.run() in Widget.render


@unittest.skipUnless(rg.tree_sitter_available(),
                     "tree-sitter grammar pack not installed")
class ResolutionHierarchyTest(unittest.TestCase):
    """Precise self/super/inheritance resolution (needs qualified methods)."""

    SRC = (
        "class A:\n"
        "    def run(self):\n"
        "        return self.step()\n"
        "    def step(self):\n"
        "        return 1\n"
        "\n"
        "class B:\n"
        "    def run(self):\n"
        "        return self.step()\n"
        "    def step(self):\n"
        "        return 2\n"
        "\n"
        "class C(A):\n"
        "    def go(self):\n"
        "        return self.run()\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "m.py").write_text(self.SRC, encoding="utf-8")
        self.repo = rg.build_repo(self.root, use_tree_sitter=True, edges=True)
        self.resolved = rg.resolve_edges(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def _edge(self, src):
        return next(r for r in self.resolved if r.src == src)

    def test_self_resolves_to_own_class_method(self):
        # A.run -> A.step (line 4), NOT B.step (line 10)
        a = self._edge("A.run")
        self.assertEqual((a.dst_file, a.dst_line, a.conf, a.prov),
                         ("m.py", 4, "high", "self"))
        b = self._edge("B.run")
        self.assertEqual(b.dst_line, 10)  # B.run -> B.step, disambiguated

    def test_inherited_method_resolves_through_base(self):
        # C(A).go calls self.run() -> A.run (line 2), inherited
        c = self._edge("C.go")
        self.assertEqual((c.dst_file, c.dst_line, c.conf), ("m.py", 2, "high"))

    def test_callers_strict_disambiguates(self):
        # callers of step at high confidence: A.run and B.run, each correct
        rows = rg.find_callers(self.repo, "step", min_conf="high")
        by_caller = {r[2]: r for r in rows}
        self.assertIn("A.run", by_caller)
        self.assertIn("B.run", by_caller)
        self.assertTrue(all(r[4] == "high" for r in rows))

    def test_impact_follows_inheritance(self):
        rows, files, tests = rg.find_impact(self.repo, "run", min_conf="high")
        callers = {r[1] for r in rows}
        self.assertIn("C.go", callers)  # reaches the inherited caller


class ImportResolutionTest(unittest.TestCase):
    """Phase 2: import bindings → module→file resolution beats name fallback."""

    PY_FILES = {
        "pkg/util.py": "def helper():\n    return 1\n",
        "app.py": "from pkg.util import helper\ndef run():\n    return helper()\n",
        # decoy: a same-named def elsewhere — name-fallback would be ambiguous,
        # import resolution must still pick pkg/util.py.
        "decoy.py": "def helper():\n    return 99\n",
    }
    JS_FILES = {
        "lib/math.js": "export function add(a, b) { return a + b; }\n",
        "main.js": "import { add } from './lib/math';\n"
                   "function compute() { return add(1, 2); }\n",
    }

    def _build(self, files):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return rg.build_repo(root, use_ctags=False, edges=True)

    def tearDown(self):
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_python_import_binding_extracted(self):
        repo = self._build(self.PY_FILES)
        app = next(f for f in repo.files if f.rel_path == "app.py")
        self.assertIn(("helper", "pkg.util"), app.import_bindings)

    def test_python_call_resolves_through_import(self):
        repo = self._build(self.PY_FILES)
        hit = [r for r in rg.resolve_edges(repo)
               if r.src == "run" and r.dst == "helper"][0]
        self.assertEqual((hit.dst_file, hit.conf, hit.prov),
                         ("pkg/util.py", "high", "import"))  # not decoy.py

    def test_python_module_resolver(self):
        fs = {"pkg/util.py", "pkg/__init__.py", "app.py"}
        self.assertEqual(rg._resolve_py_module("app.py", "pkg.util", fs),
                         "pkg/util.py")
        # relative import climbs from the importer's package
        self.assertEqual(
            rg._resolve_py_module("pkg/app.py", ".util", {"pkg/util.py"}),
            "pkg/util.py")

    def test_js_import_binding_and_resolution(self):
        repo = self._build(self.JS_FILES)
        main = next(f for f in repo.files if f.rel_path == "main.js")
        self.assertIn(("add", "./lib/math"), main.import_bindings)
        hit = [r for r in rg.resolve_edges(repo) if r.dst == "add"][0]
        self.assertEqual((hit.dst_file, hit.conf, hit.prov),
                         ("lib/math.js", "high", "import"))

    def test_js_module_resolver_extensions_and_index(self):
        fs = {"lib/math.ts", "lib/widget/index.tsx", "main.ts"}
        self.assertEqual(rg._resolve_js_module("main.ts", "./lib/math", fs),
                         "lib/math.ts")
        self.assertEqual(rg._resolve_js_module("main.ts", "./lib/widget", fs),
                         "lib/widget/index.tsx")
        self.assertIsNone(rg._resolve_js_module("main.ts", "react", fs))

    def test_bindings_round_trip_json(self):
        repo = self._build(self.PY_FILES)
        files = rg.files_from_dict(rg.repo_to_dict(repo))
        self.assertIn(("helper", "pkg.util"), files["app.py"].import_bindings)


class GoJavaResolutionTest(unittest.TestCase):
    """Phase 3: Go package-qualified + Java imported-class call resolution."""

    GO_FILES = {
        "util/util.go": "package util\nfunc Helper() int { return 1 }\n",
        "cmd/main.go": ('package main\nimport "github.com/me/proj/util"\n'
                        "func run() int { return util.Helper() }\n"),
        # decoy Helper elsewhere -> name fallback would be ambiguous
        "decoy.go": "package decoy\nfunc Helper() int { return 99 }\n",
    }
    JAVA_FILES = {
        "com/lib/Util.java": ("package com.lib;\npublic class Util {\n"
                              "    public static int helper() { return 1; }\n}\n"),
        "com/app/Main.java": ("package com.app;\nimport com.lib.Util;\n"
                              "public class Main {\n"
                              "    int run() { return Util.helper(); }\n}\n"),
    }

    def _build(self, files, **kw):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for rel, content in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return rg.build_repo(root, use_ctags=False, edges=True, **kw)

    def tearDown(self):
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_go_import_binding(self):
        repo = self._build(self.GO_FILES)
        main = next(f for f in repo.files if f.rel_path == "cmd/main.go")
        self.assertIn(("util", "github.com/me/proj/util"), main.import_bindings)

    def test_go_grouped_import_binding(self):
        bindings = rg.extract_import_bindings(
            ['import (', '    "a/b/foo"', '    bar "c/d/baz"', ')'], "go")
        self.assertIn(("foo", "a/b/foo"), bindings)
        self.assertIn(("bar", "c/d/baz"), bindings)

    def test_go_package_call_resolves(self):
        # util.Helper() resolves to util/util.go, NOT the decoy
        repo = self._build(self.GO_FILES)
        hit = [r for r in rg.resolve_edges(repo)
               if r.src == "run" and r.dst == "Helper"][0]
        self.assertEqual((hit.dst_file, hit.conf, hit.prov),
                         ("util/util.go", "high", "import"))

    def test_go_package_resolver(self):
        dir_files = {"util": ["util/util.go"], "proj/util": ["proj/util/x.go"]}
        self.assertEqual(
            rg._resolve_go_package("github.com/me/proj/util", dir_files),
            ["proj/util/x.go"])  # longest-suffix wins over bare "util"
        self.assertEqual(rg._resolve_go_package("net/http", dir_files), [])

    def test_java_import_binding(self):
        bindings = rg.extract_import_bindings(
            ["import com.lib.Util;", "import static com.lib.C.make;",
             "import com.lib.*;"], "java")
        self.assertIn(("Util", "com.lib.Util"), bindings)
        self.assertIn(("make", "com.lib.C"), bindings)        # static -> class
        self.assertTrue(all(b[0] != "*" for b in bindings))   # wildcard skipped

    def test_java_class_resolver(self):
        fs = {"src/main/java/com/lib/Util.java", "com/app/Main.java"}
        self.assertEqual(rg._resolve_java_class("com.lib.Util", fs),
                         ["src/main/java/com/lib/Util.java"])  # source-root suffix
        self.assertEqual(rg._resolve_java_class("java.util.List", fs), [])

    def test_python_import_as_binding(self):
        b = rg.extract_import_bindings(
            ["import numpy as np", "import os"], "python")
        self.assertIn(("np", "numpy"), b)
        self.assertIn(("os", "os"), b)


@unittest.skipUnless(rg.tree_sitter_available(),
                     "tree-sitter grammar pack not installed")
class JavaResolutionTreeSitterTest(unittest.TestCase):
    """Java method calls need a method-aware backend (regex misses them)."""

    def test_java_static_call_resolves(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for rel, content in GoJavaResolutionTest.JAVA_FILES.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        repo = rg.build_repo(root, use_tree_sitter=True, edges=True)
        hit = [r for r in rg.resolve_edges(repo) if r.dst == "helper"][0]
        self.assertEqual((hit.dst_file, hit.conf), ("com/lib/Util.java", "high"))
        self._tmp.cleanup()


class WatchSignatureTest(unittest.TestCase):
    """_repo_signature underpins --watch change detection."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_fixture(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_signature_changes_on_edit(self):
        sig1 = rg._repo_signature(self.root)
        self.assertTrue(sig1)
        # rewrite a file with a newer mtime
        target = self.root / "src" / "app.py"
        st = target.stat()
        target.write_text("# changed\n", encoding="utf-8")
        os.utime(target, (st.st_atime + 5, st.st_mtime + 5))
        sig2 = rg._repo_signature(self.root)
        self.assertNotEqual(sig1, sig2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
