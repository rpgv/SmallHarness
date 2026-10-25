"""The code library: recipe sections kept as snippet files per language, reused before writing."""
import json

from smallharness.recipe import RecipeRunner, load_recipe, save_recipe
from smallharness.snippets import language_key, open_library, similarity
from tests.helpers import WorkspaceTest

SHAPES = "target: src/shapes.py\n\narea(radius) -> float using math.pi\n\nperimeter(radius) -> float\n"


class CodeLibrary(WorkspaceTest):
    def write(self, h, name, text):
        r = load_recipe(h.cfg, save_recipe(h.cfg, name, text))
        RecipeRunner(h, r).run(permit=lambda: True)
        return load_recipe(h.cfg, r.path)

    def test_sections_are_files_and_the_output_is_built_from_them(self):
        h = self.harness()
        r = self.write(h, "shapes", SHAPES)
        lib_dir = h.cfg.state.snippets / "python"
        files = sorted(p.name for p in lib_dir.glob("*.py"))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(s.snippet and not s.reused for s in r.sections))
        manifest = json.loads((lib_dir / "manifest.json").read_text())
        area = manifest[r.sections[0].snippet]
        self.assertEqual((area["description"], area["recipes"]), ("area(radius) -> float using math.pi", ["shapes"]))
        snippet = lib_dir / f"{r.sections[0].snippet}.py"
        snippet.write_text(snippet.read_text().replace("return 1", "return 3.14 * radius ** 2"))
        self.assertIn("return 3.14 * radius ** 2", r.assemble())             # the file is built from the snippets
        self.assertTrue(r.assemble().startswith("import math\n"))             # imports still gathered

    def test_a_near_duplicate_section_is_reused_without_the_model(self):
        h = self.harness()
        first = self.write(h, "shapes", SHAPES)
        n = len(self.backend.prompts)
        second = self.write(h, "circles", "target: src/circles.py\n\narea(radius) -> float using math.pi\n\n"
                                          "volume(radius) -> float\n")
        self.assertEqual(len(self.backend.prompts), n + 1)                     # only volume() was written
        area = second.sections[0]
        self.assertTrue(area.reused)
        self.assertEqual(area.snippet, first.sections[0].snippet)
        self.assertIn("reused", area.note)
        self.assertIn("def area", (self.ws / "src/circles.py").read_text())
        lib = open_library(h.cfg, "src/x.py")
        self.assertEqual(lib.get(area.snippet).recipes, ["shapes", "circles"])
        self.assertNotIn("area(radius)", self.backend.prompts[-1].split("SECTION 2:")[1].split("LIBRARY")[0])

    def test_similar_snippets_are_offered_and_can_be_chosen(self):
        h = self.harness()
        first = self.write(h, "shapes", SHAPES)
        sid = first.sections[1].snippet                                       # perimeter
        self.backend.script.append(f"REUSE {sid}")
        r = self.write(h, "rings", "target: src/rings.py\n\nperimeter of a ring, given radius r, as a float\n")
        prompt = self.backend.prompts[-1]
        self.assertIn("LIBRARY (code already written)", prompt)
        self.assertIn(f"[{sid}] perimeter(radius) -> float\ndef perimeter():", prompt)
        self.assertTrue(r.sections[0].reused)
        self.assertEqual(r.sections[0].snippet, sid)
        self.assertIn("chosen from the library", r.sections[0].note)

    def test_one_library_per_language_and_none_for_text(self):
        h = self.harness()
        self.write(h, "shapes", SHAPES)
        n = len(self.backend.prompts)
        self.write(h, "jsshapes", "target: web/shapes.js\n\narea(radius) -> float using math.pi\n")
        self.assertEqual(len(self.backend.prompts), n + 1)                     # python snippets are not JavaScript
        self.assertNotIn("LIBRARY", self.backend.prompts[-1])
        self.assertTrue((h.cfg.state.snippets / "javascript").is_dir())
        self.write(h, "doc", "target: docs/area.md\n\narea(radius) -> float using math.pi\n")
        self.assertFalse((h.cfg.state.snippets / "markdown").exists())        # text mode keeps no library
        self.assertEqual([language_key(x) for x in ("C++", "C#", "Python")], ["cpp", "csharp", "python"])

    def test_same_code_is_stored_once_and_the_library_can_be_off(self):
        h = self.harness()
        lib = open_library(h.cfg, "a.py")
        a = lib.add("def f():\n    return 1\n", "f returns one", "r1")
        b = lib.add("def f():\n    return 1", "f gives back 1", "r2")
        self.assertEqual((a.id, b.recipes), (b.id, ["r1", "r2"]))
        self.assertGreater(similarity("area of a circle from radius", "area of a circle, from the radius"), 0.85)
        h.cfg.data["recipe"]["library"] = False
        r = self.write(h, "plain", "target: p.py\n\nf returns one\n")
        self.assertEqual(r.sections[0].snippet, "")
        lib.remove(a.id)
        self.assertEqual(lib.all(), [])

    def test_a_removed_snippet_is_written_again(self):
        h = self.harness()
        r = self.write(h, "shapes", SHAPES)
        open_library(h.cfg, "src/shapes.py").remove(r.sections[0].snippet)
        again = load_recipe(h.cfg, r.path)
        self.assertEqual([s.num for s in again.stale()], [1])
        n = len(self.backend.prompts)
        RecipeRunner(h, again).run(permit=lambda: True)
        self.assertEqual(len(self.backend.prompts), n + 1)


class Terminal(WorkspaceTest):
    def test_write_then_keep_going_and_browse_the_library(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        from smallharness import cli
        h = self.harness()
        save_recipe(h.cfg, "shapes", SHAPES)
        save_recipe(h.cfg, "circles", "target: src/circles.py\n\narea(radius) -> float using math.pi\n")
        typed = iter(["/write shapes", "y", "/write circles", "y", "/library", "/library python area", "/exit"])
        with mock.patch("builtins.input", lambda _="": next(typed)), redirect_stdout(io.StringIO()) as out:
            cli.RecipeShell(h, cli.TerminalEvents(), True).run()
        text = out.getvalue()
        self.assertIn("♻ section 1: reused area-", text)                     # the second recipe reused it
        self.assertIn("python  2 snippet(s)", text)
        self.assertIn("area(radius) -> float using math.pi  · shapes, circles", text)
