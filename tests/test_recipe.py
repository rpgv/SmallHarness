"""Recipes: pseudo code to code, one clean-context call per section."""
import io
import unittest
from contextlib import redirect_stdout

from smallharness import cli
from smallharness.agent import Events
from smallharness.config import Config
from smallharness.errors import Invalid
from smallharness.recipe import RecipeRunner, load_recipe, parse_recipe, save_recipe
from tests.helpers import WorkspaceTest

RECIPE = """target: src/shapes.py

area(radius) -> float
returns pi r squared, using math.pi

perimeter(radius) -> float

describe(radius) -> str
"""


class Asks(Events):
    def __init__(self, allow=True):
        self.allow, self.asked = allow, []

    def permit(self, tool, args):
        self.asked.append((tool.name, args))
        return self.allow


class Base(WorkspaceTest):
    def recipe(self, h, text=RECIPE, name="shapes"):
        return load_recipe(h.cfg, save_recipe(h.cfg, name, text))



class Parsing(unittest.TestCase):
    def test_sections_and_target(self):
        header, sections = parse_recipe("\n\ntarget: a.py\nmode: text\n\none\ntwo\n  \t\nthree\r\n\r\nfour")
        self.assertEqual(header, {"target": "a.py", "mode": "text"})
        self.assertEqual(sections, ["one\ntwo", "three", "four"])
        self.assertEqual(parse_recipe("just one")[0], {})


class Recipes(Base):
    def test_each_section_alone_into_one_file_asked_once(self):
        events = Asks()
        h = self.harness(events=events)
        r = self.recipe(h)
        res = RecipeRunner(h, r, events).run()
        self.assertEqual((res.written, res.failed, res.reason), ([1, 2, 3], [], "finished"))
        self.assertEqual(len(events.asked), 1)                                # once, for all three
        self.assertEqual(events.asked[0][0], "risky_write_file")
        code = (self.ws / "src/shapes.py").read_text()
        self.assertTrue(code.startswith("import math\n"))                     # imports gathered at the top
        self.assertLess(code.index("def area"), code.index("def perimeter"))
        self.assertLess(code.index("def perimeter"), code.index("def describe"))
        compile(code, "shapes.py", "exec")
        texts = [s.text for s in r.sections]
        for i, prompt in enumerate(self.backend.prompts):
            self.assertIn(texts[i], prompt)
            for j, other in enumerate(texts):
                if j != i:
                    self.assertNotIn(other, prompt)                           # sees only its section
            self.assertNotIn("safe_read_file", prompt)                       # and only the write tool

    def test_denied_writes_nothing(self):
        events = Asks(allow=False)
        h = self.harness(events=events)
        res = RecipeRunner(h, self.recipe(h), events).run()
        self.assertEqual(res.reason, "denied")
        self.assertEqual(self.backend.prompts, [])
        self.assertFalse((self.ws / "src/shapes.py").exists())

    def test_reruns_write_only_new_or_changed_sections(self):
        h = self.harness()
        r = self.recipe(h)
        RecipeRunner(h, r).run(permit=lambda: True)
        n = len(self.backend.prompts)
        self.assertEqual(RecipeRunner(h, load_recipe(h.cfg, r.path)).run(permit=lambda: True).reason,
                         "nothing to write")
        self.assertEqual(len(self.backend.prompts), n)
        save_recipe(h.cfg, "shapes", RECIPE.replace("perimeter(radius) -> float", "circumference(radius) -> float"))
        r2 = load_recipe(h.cfg, r.path)
        self.assertEqual([s.num for s in r2.stale()], [2])
        res = RecipeRunner(h, r2).run(permit=lambda: True)
        self.assertEqual(res.written, [2])
        self.assertEqual(len(self.backend.prompts), n + 1)
        code = (self.ws / "src/shapes.py").read_text()
        self.assertIn("def circumference", code)
        self.assertNotIn("def perimeter", code)
        self.assertIn("def describe", code)                                   # the others are kept
        res = RecipeRunner(h, load_recipe(h.cfg, r.path), only=[3]).run(permit=lambda: True)
        self.assertEqual(res.written, [3])
        res = RecipeRunner(h, load_recipe(h.cfg, r.path), everything=True).run(permit=lambda: True)
        self.assertEqual(res.written, [1, 2, 3])

    def test_restrictions_and_retries(self):
        bad_tool = '<tool>{"name": "risky_run_command", "args": {"command": "ls"}}</tool>'
        other_file = '<tool>{"name": "risky_write_file", "args": {"path": "elsewhere.py", "content": "x = 1"}}</tool>'
        fenced = "Here it is:\n```python\ndef area(r):\n    return 3.14 * r * r\n```"
        broken = '<tool>{"name": "risky_write_file", "args": {"path": "src/shapes.py", "content": "def f(:\\n"}}</tool>'
        h = self.harness([bad_tool, fenced, other_file, other_file, broken, broken])
        res = RecipeRunner(h, self.recipe(h)).run(permit=lambda: True)
        self.assertEqual((res.written, res.failed), ([1], [2, 3]))
        p = self.backend.prompts
        self.assertIn("risky_run_command is not available", p[1])            # retry says why
        self.assertIn("not elsewhere.py", p[3])
        self.assertFalse((self.ws / "elsewhere.py").exists())
        code = (self.ws / "src/shapes.py").read_text()
        self.assertIn("return 3.14 * r * r", code)                            # from the code block
        self.assertIn("# recipe section 2 failed: This recipe writes only src/shapes.py", code)
        self.assertIn("# recipe section 3 failed: The code has a syntax error", code)
        self.assertIn("def f(:", code)                                        # kept, to be fixed

    def test_text_mode_writes_markdown(self):
        h = self.harness()
        r = load_recipe(h.cfg, save_recipe(h.cfg, "intro", "Purpose: why the project exists\n\nAudience: who reads it",
                                           target="docs/intro.md"))
        self.assertEqual(r.mode, "text")                                   # by the .md target
        res = RecipeRunner(h, r).run(permit=lambda: True)
        self.assertEqual(res.written, [1, 2])
        doc = (self.ws / "docs/intro.md").read_text()
        self.assertEqual(doc, "The Purpose section, in a short paragraph.\n\nThe Audience section, in a short paragraph.\n")
        self.assertIn("Markdown prose", self.backend.prompts[0])
        self.assertNotIn("imports", self.backend.prompts[0])
        code = load_recipe(h.cfg, r.path, mode="code")                     # the toggle
        self.assertEqual(code.mode, "code")
        self.assertEqual(len(code.stale()), 2)                             # another mode: written again
        p = save_recipe(h.cfg, "intro", "x\n\ny", target="docs/intro.md", mode="code")
        self.assertTrue(p.read_text().startswith("target: docs/intro.md\nmode: code\n"))

    def test_sections_in_parallel(self):
        from unittest import mock
        h = self.harness()
        r = self.recipe(h)
        with mock.patch.object(type(h.pool), "capacity", new_callable=mock.PropertyMock, return_value=3):
            res = RecipeRunner(h, r).run(permit=lambda: True, parallel=True)
        self.assertEqual(len(h.pool.instances), 3)                          # as many as RAM allows
        self.assertEqual(res.written, [1, 2, 3])
        code = (self.ws / "src/shapes.py").read_text()
        self.assertLess(code.index("def area"), code.index("def describe"))  # still in recipe order

    def test_target_must_be_in_the_workspace(self):
        h = self.harness()
        with self.assertRaises(Invalid):
            RecipeRunner(h, self.recipe(h, RECIPE.replace("src/shapes.py", "../outside.py")))
        with self.assertRaises(ValueError):
            self.recipe(h, "no target here\n\nanother")

    def test_a_recipe_file_in_the_workspace(self):
        h = self.harness()
        (self.ws / "docs").mkdir()
        (self.ws / "docs/r.txt").write_text("one() -> int\n\ntwo() -> int\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.RecipeShell(h, cli.TerminalEvents(), True).write("docs/r.txt", "lib.py", "code", assume_yes=True)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("def one", (self.ws / "lib.py").read_text())
        self.assertEqual(sorted(p.name for p in (self.ws / "docs").iterdir()), ["r.txt"])   # cache kept elsewhere

    def test_cli_subcommand(self):
        save_recipe(Config.load(self.ws), "calc", "target: calc.py\n\nadd(a, b) -> a + b\n\nsub(a, b) -> a - b\n")
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["-w", str(self.ws), "--trust", "-b", "mock", "--no-color", "recipe", "calc", "-y"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("def add", (self.ws / "calc.py").read_text())
        with redirect_stdout(io.StringIO()) as again:
            cli.main(["-w", str(self.ws), "--trust", "-b", "mock", "--no-color", "recipe", "calc", "-y"])
        self.assertIn("written already", again.getvalue())


if __name__ == "__main__":
    unittest.main()
