"""Changing the model and settings while running, folder indexing with OCR, the title."""
import contextlib
import copy
import io
import sys
import tempfile
import unittest
from pathlib import Path

from smallharness import ui
from smallharness.backends import MockBackend
from smallharness.cli import Shell, TerminalEvents
from smallharness.config import DEFAULTS, Config, changed_keys, parse_value
from smallharness.context import RagStore
from smallharness.engines import engine
from tests.helpers import WorkspaceTest


class Base(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.loaded: list[str] = []

    def harness(self, **model):
        def factory():
            path = h.cfg["model"]["path"]

            class Loading(MockBackend):
                def load(inner):
                    if path.endswith("bad.litertlm"):
                        raise RuntimeError("cannot load bad.litertlm")
                    self.loaded.append(path)
            return Loading(["ok"] * 50)
        h = super().harness(factory=factory, start=False, model=model)
        h.start()
        return h



class Settings(Base):
    def test_parse_value(self):
        self.assertEqual(parse_value("4096"), 4096)
        self.assertEqual(parse_value("true"), True)
        self.assertEqual(parse_value('["a", "b"]'), ["a", "b"])
        self.assertEqual(parse_value("gemma"), "gemma")  # bare words stay strings

    def test_with_values_rejects_unknown_keys(self):
        cfg = Config.load(self.ws)
        with self.assertRaises(KeyError):
            cfg.with_values({"model.colour": 1})
        data = cfg.with_values({"context.retrieve_k": 5})
        self.assertEqual(changed_keys(cfg.data, data), {"context.retrieve_k"})
        self.assertEqual(cfg["context"]["retrieve_k"], DEFAULTS["context"]["retrieve_k"])  # copy, not in place

    def test_reload_reads_file_and_keeps_flags(self):
        toml = self.ws / "smallharness.toml"
        toml.write_text("[context]\nretrieve_k = 2\n[model]\npath = 'from-file.litertlm'\n")
        cfg = Config.load(self.ws, overrides={"model": {"path": "from-flag.litertlm"}})
        toml.write_text("[context]\nretrieve_k = 7\n[model]\npath = 'edited.litertlm'\n")
        data, files = cfg.read_files()
        self.assertEqual(data["context"]["retrieve_k"], 7)
        self.assertEqual(data["model"]["path"], "from-flag.litertlm")  # -m still wins
        self.assertEqual(files, [toml])

    def test_live_setting_does_not_restart(self):
        h = self.harness(path="a.litertlm")
        first = h.pool.instances[0]
        self.assertEqual(h.reconfigure(h.cfg.with_values({"context.retrieve_k": 9, "tools.auto": False})), [])
        self.assertIs(h.pool.instances[0], first)
        self.assertFalse(h.auto_tools)


class SwitchModel(Base):
    def test_switch_restarts_and_keeps_session(self):
        h = self.harness(path="a.litertlm")
        h.session.add("user", "remember the word pineapple")
        done = h.reconfigure(h.cfg.with_values({"model.path": "b.litertlm", "model.context_tokens": 8192}))
        self.assertEqual(self.loaded, ["a.litertlm", "b.litertlm"])
        self.assertIn("model restarted", done[0])
        self.assertEqual(h.ctx.budget, 8192 - h.cfg["model"]["max_output_tokens"])
        self.assertIn("pineapple", h.ctx.build().prompt)  # the harness owns the conversation
        self.assertEqual(h.agent().respond("hi", stream=False), "ok")

    def test_failed_switch_restores_previous_model(self):
        h = self.harness(path="a.litertlm")
        with self.assertRaisesRegex(RuntimeError, "kept the previous model"):
            h.reconfigure(h.cfg.with_values({"model.path": "bad.litertlm"}))
        self.assertEqual(h.cfg["model"]["path"], "a.litertlm")
        self.assertEqual(self.loaded, ["a.litertlm", "a.litertlm"])
        self.assertEqual(len(h.pool.instances), 1)

    def test_smaller_window_refreshes_context(self):
        h = self.harness(path="a.litertlm", context_tokens=8192)
        for i in range(30):
            h.session.add("user", f"message {i} " + "word " * 60)
        done = h.reconfigure(h.cfg.with_values({"model.context_tokens": 2048}))
        self.assertTrue(any("context refreshed" in d for d in done))
        built = h.ctx.build()
        self.assertLessEqual(built.total, built.budget)

    def test_lower_instance_limit_stops_extra_instances(self):
        h = self.harness(path="a.litertlm")
        h.pool.spawn(2)
        self.assertEqual(len(h.pool.instances), 3)
        done = h.reconfigure(h.cfg.with_values({"resources.manage_ram": False}))
        self.assertEqual(len(h.pool.instances), 1)
        self.assertIn("stopped 2", done[0])

    def test_model_command_arguments(self):
        h = self.harness(path="a.litertlm")
        repl = Shell(h, TerminalEvents())
        switch = lambda words: engine(h.cfg).switch_values(words)[0]   # noqa: E731
        self.assertEqual(switch(["b.litertlm", "ctx=2048", "template=gemma"]),
                         {"model.path": "b.litertlm", "model.name": "", "model.context_tokens": 2048,
                          "model.template": "gemma"})
        self.assertEqual(switch(["out=512"]), {"model.max_output_tokens": 512})
        for bad in (["ctx=big"], ["b.litertlm", "colour=red"], ["template=llama"]):
            with self.assertRaises(ValueError):
                switch(bad)
        with contextlib.redirect_stdout(io.StringIO()):
            repl.cmd_model("b.litertlm ctx=2048")
        self.assertEqual((h.cfg.model_name, h.cfg["model"]["context_tokens"]), ("b", 2048))

    def test_server_mode_target_is_model_id(self):
        h = self.harness(path="a.litertlm")
        h.cfg.data["model"]["backend"] = "litert"
        h.cfg.data["litert"]["server"]["base_url"] = "http://127.0.0.1:9"
        values, warnings = engine(h.cfg).switch_values(["gemma-4-E4B-it"])
        self.assertEqual(values, {"litert.server.model": "gemma-4-E4B-it", "model.name": "gemma-4-E4B-it"})


FAKE_PDFTOTEXT = """import sys
# page 1 has a text layer, page 2 is a scan with none
sys.stdout.write("Invoice from ACME for consulting services rendered\\f\\f")
"""
FAKE_PDFTOPPM = """import sys
args = sys.argv[1:]
page, out = args[args.index("-f") + 1], args[-1]
open(f"{out}-{page}.png", "w").write(f"scanned page {page} mentions pineapple")
"""
FAKE_TESSERACT = """import sys
sys.stdout.write("OCR " + open(sys.argv[1]).read())
"""


class FolderIndexing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.root, bin_ = d / "docs", d / "bin"
        self.root.mkdir()
        bin_.mkdir()
        (self.root / "notes.txt").write_text("plain notes about bananas\n")
        (self.root / "build.sh").write_text("#!/bin/sh\necho building\n")
        (self.root / "data.json").write_text('{"ignored": true}')
        (self.root / "sub").mkdir()
        (self.root / "sub" / "receipt.png").write_text("receipt total 42 euros")
        (self.root / "report.pdf").write_text("%PDF fake")
        self.rag = copy.deepcopy(DEFAULTS["rag"])
        for key, src in (("pdf_text", FAKE_PDFTOTEXT), ("pdf_render", FAKE_PDFTOPPM), ("image", FAKE_TESSERACT)):
            script = bin_ / f"{key}.py"
            script.write_text(src)
            argv = {"pdf_text": ["{input}"],
                    "pdf_render": ["-f", "{page}", "-l", "{page}", "{input}", "{output}"],
                    "image": ["{input}"]}[key]
            self.rag["ocr"][key] = [sys.executable, str(script), *argv]
        self.store = RagStore(d / "idx.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_folder_with_text_pdf_and_images(self):
        seen = []
        res = self.store.add_path(self.root, self.root, self.rag, progress=seen.append)
        self.assertEqual((res.files, res.ignored, res.skipped), (4, 1, []))  # data.json is not accepted
        self.assertIn("OCR report.pdf", seen)
        self.assertEqual(self.store.search("receipt euros")[0].source, "sub/receipt.png")
        pdf = self.store.search("pineapple scanned")[0]
        self.assertEqual(pdf.source, "report.pdf")
        self.assertIn("[page 2]", pdf.text)
        self.assertIn("ACME", pdf.text)  # page 1 kept its own text

    def test_always_mode_ocrs_every_page(self):
        self.rag["ocr"]["pdf_mode"] = "always"
        self.store.add_path(self.root / "report.pdf", self.root, self.rag)
        text = self.store.search("scanned page")[0].text
        self.assertIn("scanned page 1", text)
        self.assertNotIn("ACME", text)

    def test_missing_ocr_program_is_reported(self):
        self.rag["ocr"]["image"] = ["no-such-tesseract", "{input}", "stdout"]
        res = self.store.add_path(self.root, self.root, self.rag)
        reasons = dict(res.skipped)
        self.assertIn("not installed", reasons["sub/receipt.png"])
        self.assertIn("not installed", reasons["report.pdf"])  # page 2 needs OCR
        self.assertEqual(res.files, 2)

    def test_single_file_of_other_type_is_refused(self):
        res = self.store.add_path(self.root / "data.json", self.root, self.rag)
        self.assertEqual(res.files, 0)
        self.assertIn("not accepted", res.skipped[0][1])


class Title(unittest.TestCase):
    def test_first_run_only(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "cfg" / ".welcomed"
            self.assertTrue(ui.show_title("first", marker))
            self.assertFalse(ui.show_title("first", marker))
            self.assertTrue(ui.show_title("always", marker))
            self.assertFalse(ui.show_title("never", Path(d) / "other"))

    def test_title_rows_line_up(self):
        widths = {len(r.rstrip()) for r in ui.TITLE[1:]}
        self.assertLessEqual(max(widths) - min(widths), 1)


if __name__ == "__main__":
    unittest.main()
