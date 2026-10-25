"""Notes."""
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from smallharness import cli
from smallharness.context import RagStore
from smallharness.notemode import NoteMode
from smallharness.notes import NoteStore, parse_id, tags_of
from tests.helpers import WorkspaceTest


class Base(WorkspaceTest):
    def store(self):
        return NoteStore(self.ws / "notes", RagStore(self.ws / "rag.json"))



class Notes(Base):
    def test_tags(self):
        self.assertEqual(tags_of("# Heading\nbuy milk #Shopping, C# #1 a#b (#todo) #home-lab #shopping"),
                         ["shopping", "todo", "home-lab"])
        self.assertEqual([parse_id(x) for x in (3, "3", "[3]", "note:3")], [3, 3, 3, 3])
        with self.assertRaises(ValueError):
            parse_id("three")

    def test_save_index_search(self):
        s = self.store()
        a = s.add("# Deploy\nRun the deploy script with --prod #ops")
        s.add("Pineapple pizza recipe #food")
        s.add("Untagged thought about deploy windows")
        self.assertEqual(s.get(a.id).title, "Deploy")
        self.assertEqual([n.id for n, _ in s.search("deploy")][:2], [1, 3])
        self.assertEqual([n.id for n, _ in s.search("deploy", tag="#ops")], [1])
        self.assertEqual([n.id for n in s.all("food")], [2])
        self.assertEqual(s.tags(), {"ops": 1, "food": 1})
        self.assertNotIn("tags:", s.search("pizza")[0][1].text)      # index-only line hidden
        s.append(2, "with extra #cheese")
        self.assertIn("cheese", s.get(2).tags)
        s.delete(1)
        self.assertEqual([n.id for n, _ in s.search("prod")], [])
        self.assertEqual(s.add("next").id, 4)                         # numbers are not reused

    def test_files_edited_outside_are_picked_up(self):
        s = self.store()
        s.add("old text")
        s.add("to be removed")
        (self.ws / "notes" / "1.md").write_text("now about kubernetes #ops\n")
        os.utime(self.ws / "notes" / "1.md", (1, 1))
        (self.ws / "notes" / "2.md").unlink()
        s2 = self.store()
        self.assertEqual([n.id for n, _ in s2.search("kubernetes")], [1])
        self.assertNotIn("note:2", s2.rag.sources)

    def test_notes_are_part_of_the_knowledge_base(self):
        from smallharness.research import Research
        h = self.harness()
        h.notes.add("The wifi password is hunter2 #home")
        r = Research(h)
        self.assertEqual(r.find("wifi password")[0].source, "note:1")
        self.assertNotIn("safe_note_add", h.tools.tools)          # the model has no note or search tools

    def test_note_mode_needs_no_model(self):
        cfg = self.ws / "own.toml"
        cfg.write_text('[model]\nbackend = "litert"\npath = "/nonexistent.litertlm"\n')
        with redirect_stdout(io.StringIO()) as out, \
                mock.patch("builtins.input", side_effect=["first #idea", "/list", "/search first",
                                                         "/tags", "/exit"]):
            code = cli.main(["-w", str(self.ws), "-c", str(cfg), "--no-color", "notes"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("no model loaded", text)
        self.assertIn("saved [1] first #idea", text)
        self.assertIn("#idea 1", text)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["-w", str(self.ws), "-c", str(cfg), "notes", "quick", "one"]), 0)
        self.assertEqual((self.ws / ".smallharness" / "notes" / "2.md").read_text(), "quick one\n")

    def test_note_mode_line_by_line_editing(self):
        s = self.store()
        s.add("draft")
        mode = NoteMode(s)
        with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"VISUAL": "", "EDITOR": ""}), \
                mock.patch("builtins.input", side_effect=["line one #a", "line two", ".", "y"]):
            mode.command("/edit 1")
            mode.command("/delete 1")
        self.assertEqual(s.all(), [])


if __name__ == "__main__":
    unittest.main()
