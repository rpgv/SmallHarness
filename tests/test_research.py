"""Research mode: a knowledge base searched by the harness, answered from with references."""
import unittest

from smallharness.context.embed import fastembed_installed
from smallharness.research import Research
from tests.helpers import WorkspaceTest


class ResearchMode(WorkspaceTest):
    def setUp(self):
        super().setUp()
        (self.ws / "docs").mkdir()
        (self.ws / "docs" / "release.md").write_text("# Release\nThe release train leaves every Friday at noon.\n")
        (self.ws / "docs" / "billing.md").write_text("# Billing\nInvoices are sent on the first of the month.\n")

    def research(self, script=None):
        h = self.harness(script)
        r = Research(h)
        r.add("docs")
        h.notes.add("Remember: the office is in Porto. #facts")
        return h, r

    def test_answers_cite_numbers_from_the_bibliography(self):
        h, r = self.research()
        a = r.ask("when does the release train leave?")
        self.assertEqual(r.refs[0].source, "docs/release.md")
        self.assertEqual(a.text, "From the excerpts: see [1].")         # numbers only, no sources in the text
        self.assertEqual(a.refs, [1])
        bib = r.bibliography_path.read_text()
        self.assertIn("[1] docs/release.md:1", bib)
        self.assertIn("every Friday", bib)
        prompt = self.backend.prompts[-1]
        self.assertIn("EXCERPTS:\n[1] (docs/release.md)", prompt)
        self.assertNotIn("TOOLS", prompt)                                  # a clean context: no tools
        self.assertNotIn("CONVERSATION:\nUSER: when", prompt.split("QUESTION")[0])
        again = r.ask("what day is the release?")
        self.assertEqual(again.refs, [1])                                  # the same excerpt keeps its number

    def test_nothing_found_needs_no_model(self):
        h, r = self.research()
        n = len(self.backend.prompts)
        a = r.ask("kubernetes autoscaling")
        self.assertEqual((a.refs, len(self.backend.prompts)), ([], n))

    def test_scope_limits_the_search(self):
        h, r = self.research()
        r.set_scope(["docs/billing.md"])
        self.assertTrue(r.ask("release train").text.startswith("Nothing in the knowledge base"))
        r.set_scope([])
        self.assertEqual(r.ask("release train").refs, [1])
        notes = [x for x in r.library() if x["kind"] == "note"]
        self.assertEqual(notes[0]["title"], "Remember: the office is in Porto. #facts")
        r.set_scope([notes[0]["source"]])
        self.assertEqual(r.refs[r.ask("where is the office").refs[0] - 1].source, notes[0]["source"])

    def test_attached_answers_are_kept_in_mind(self):
        h, r = self.research()
        first = r.ask("when does the release train leave?")
        r.ask("when are invoices sent?")
        self.assertNotIn("ATTACHED", self.backend.prompts[-1])           # clean by default
        r.attach(first.num)
        r.ask("when are invoices sent?")
        prompt = self.backend.prompts[-1]
        self.assertIn("ATTACHED", prompt)
        self.assertIn("Q: when does the release train leave?\nA: From the excerpts: see .", prompt)
        r.detach(first.num)
        r.ask("when are invoices sent?")
        self.assertNotIn("ATTACHED", self.backend.prompts[-1])

    def test_remove_and_uploads(self):
        h, r = self.research()
        r.add_upload("manual.md", b"Press the red button to stop the line.\n")
        self.assertIn(".smallharness/library/manual.md", h.project_rag.sources)
        self.assertEqual(r.refs[r.ask("how do I stop the line").refs[0] - 1].source, ".smallharness/library/manual.md")
        r.remove(".smallharness/library/manual.md")
        self.assertFalse((h.cfg.state.library / "manual.md").exists())
        self.assertNotIn(".smallharness/library/manual.md", h.project_rag.sources)


@unittest.skipUnless(fastembed_installed(), "fastembed is not installed")
class ResearchWithFastembed(ResearchMode):
    """The same, searching by meaning too: bge scores even unrelated text 0.35-0.5, and
    an unrelated question must still find nothing."""

    def harness(self, script=None, **overrides):
        return super().harness(script, rag={"embed": {"backend": "fastembed"}}, **overrides)
