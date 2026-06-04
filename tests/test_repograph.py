"""Tests for repograph against a temp fixture repo.

Stdlib unittest only (no pytest needed). Run with:
    python -m unittest discover -s repograph/tests
or:
    python repograph/tests/test_repograph.py
"""

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
        cache = {f.rel_path: f for f in self.repo.files}
        # Re-run with the full cache, nothing changed → everything reused.
        again = rg.build_repo(self.root, cache=cache)
        self.assertEqual(again.analyzed, 0)
        self.assertEqual(again.reused, len(self.repo.files))
        self.assertEqual(again.dropped, 0)

        # Change one file, delete another, then re-run with the old cache.
        (self.root / "src" / "app.py").write_text(
            "def main():\n    pass\n\n\ndef extra():\n    pass\n", encoding="utf-8"
        )
        (self.root / "lib" / "widget.js").unlink()
        cache = {f.rel_path: f for f in again.files}
        third = rg.build_repo(self.root, cache=cache)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
