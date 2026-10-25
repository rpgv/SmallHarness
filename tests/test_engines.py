"""The engine module: the one place that knows LiteRT from Ollama."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smallharness.config import Config
from smallharness.engines import InUse, LiteRTEngine, NotFound, OllamaEngine, engine


class Engines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()
        self.dir = self.ws / "models"
        self.dir.mkdir()
        env = mock.patch.dict(os.environ, {"SMALLHARNESS_TRUST_FILE": str(self.ws / "trusted.json")})
        env.start()
        self.addCleanup(env.stop)

    def cfg(self, **model):
        return Config.load(self.ws, overrides={"model": {"backend": "mock", **model},
                                               "models": {"dir": str(self.dir)}})

    def test_choice_and_identity(self):
        self.assertIsInstance(engine(self.cfg()), LiteRTEngine)
        ollama = self.cfg(backend="ollama")
        ollama.data["ollama"]["model"] = "qwen3:0.6b"
        self.assertIsInstance(engine(ollama), OllamaEngine)
        self.assertEqual(ollama.model_name, "qwen3:0.6b")
        self.assertEqual(engine(ollama).server_url(), ollama["ollama"]["base_url"])
        self.assertEqual(self.cfg(path=str(self.dir / "tiny.litertlm")).model_name, "tiny")

    def test_listing_and_delete_rules(self):
        (self.dir / "a.litertlm").write_bytes(b"x" * 10)
        (self.dir / "b.litertlm").write_bytes(b"y" * 20)
        cfg = self.cfg(path=str(self.dir / "a.litertlm"))
        eng = engine(cfg)
        rows = {m.name: m for m in eng.listing().models}
        self.assertTrue(rows["a"].active)
        self.assertFalse(rows["b"].active)
        with self.assertRaises(InUse):
            eng.plan_delete("a", in_use=True)
        self.assertTrue(eng.plan_delete("a").active)       # allowed when not loaded, but flagged
        with self.assertRaises(NotFound):
            eng.plan_delete("nope")
        d = eng.plan_delete("b")
        self.assertEqual((d.items, d.size), (["b.litertlm"], 20))
        self.assertEqual(eng.delete(d).freed, 20)
        self.assertFalse((self.dir / "b.litertlm").exists())

    def test_switch_warnings_are_returned_not_printed(self):
        cfg = self.cfg()
        cfg.data["model"]["backend"] = "litert"
        cfg.data["litert"]["server"]["base_url"] = "http://127.0.0.1:9"
        (self.dir / "m.litertlm").write_bytes(b"x")
        cfg.data["models"]["auto_import"] = False
        values, warnings = engine(cfg).switch_values([str(self.dir / "m.litertlm")])
        self.assertEqual(values["model.path"], str(self.dir / "m.litertlm"))
        self.assertIn("server mode", warnings[0])
        with self.assertRaises(ValueError):
            engine(cfg).switch_values(["m", "colour=red"])


if __name__ == "__main__":
    unittest.main()
