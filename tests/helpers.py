"""What most tests need: an empty workspace, and a harness on the mock model in it."""
import tempfile
import unittest
from pathlib import Path

from smallharness.agent import Events, Harness
from smallharness.backends import MockBackend
from smallharness.config import Config


class WorkspaceTest(unittest.TestCase):
    """Each test gets an empty workspace folder, `self.ws`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()

    def harness(self, script=None, events=None, factory=None, start=True, **overrides) -> Harness:
        """A Harness on the mock backend. `script`: the mock's replies in order (it is
        `self.backend`); `overrides`: settings by section, e.g. model={"path": "a.litertlm"}."""
        model = {"backend": "mock", **overrides.pop("model", {})}
        rag = overrides.pop("rag", {})      # keywords only unless asked: the same results with fastembed installed
        rag["embed"] = {"backend": "none", **rag.get("embed", {})}
        self.cfg = Config.load(self.ws, overrides={"model": model, "rag": rag, **overrides})
        self.backend = MockBackend(script)
        h = Harness(self.cfg, events or Events(), backend_factory=factory or (lambda: self.backend))
        if start:
            h.start()
        self.addCleanup(h.close)
        return h
