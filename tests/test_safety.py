"""Workspace containment of the safe_* tools, workspace trust, and stopping the model
servers the harness launches on every way out."""
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from smallharness import cli
from smallharness.agent import Harness
from smallharness.backends import MockBackend, server
from smallharness.backends.server import ManagedServerBackend
from smallharness.config import DEFAULTS, Config
from smallharness.tools import ToolContext, ToolRegistry

REPO = Path(__file__).resolve().parent.parent


def allow(tool, args):
    return True


class GlobContainment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.ws, self.outside = root / "ws", root / "outside"
        self.ws.mkdir()
        self.outside.mkdir()
        (self.ws / "a.txt").write_text("hello pineapple\n")
        (self.outside / "secret.txt").write_text("the password is pineapple\n")
        self.ctx = ToolContext(self.ws)
        self.tools = ToolRegistry().load()

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, name, **args):
        return self.tools.execute(name, args, self.ctx, allow)

    def test_parent_patterns_are_refused(self):
        for name, key in (("safe_list_files", "pattern"), ("safe_search_files", "glob")):
            for pattern in ("../*", "../outside/*", "sub/../../*", "..\\outside\\*", "/etc/*", "**/../../*"):
                extra = {"query": "pineapple"} if name == "safe_search_files" else {}
                out = self.run_tool(name, **{key: pattern}, **extra)
                self.assertFalse(out.ok, (name, pattern, out.text))
                self.assertIn("inside the workspace", out.text)
                self.assertNotIn("password", out.text)

    def test_symlinks_out_of_the_workspace_are_skipped(self):
        try:
            (self.ws / "linkdir").symlink_to(self.outside, target_is_directory=True)
            (self.ws / "linkfile.txt").symlink_to(self.outside / "secret.txt")
            (self.ws / "inner.txt").symlink_to(self.ws / "a.txt")
        except OSError:
            self.skipTest("symlinks not supported here")
        found = self.run_tool("safe_search_files", query="pineapple")
        self.assertTrue(found.ok)
        self.assertNotIn("password", found.text)
        self.assertIn("a.txt:1", found.text)
        self.assertIn("inner.txt:1", found.text)          # a link that stays inside is fine
        listed = self.run_tool("safe_list_files", pattern="**/*").text
        self.assertNotIn("linkfile.txt", listed)
        self.assertNotIn("secret.txt", listed)
        self.assertIn("inner.txt", listed)

    def test_ordinary_patterns_still_work(self):
        (self.ws / "src").mkdir()
        (self.ws / "src" / "m.py").write_text("x = 'pineapple'\n")
        self.assertIn("src/m.py", self.run_tool("safe_list_files", pattern="**/*.py").text)
        self.assertIn("m.py:1", self.run_tool("safe_search_files", query="pineapple", glob="src/*.py").text)
        self.assertIn("m.py", self.run_tool("safe_list_files", pattern="*.py", path="src").text)


# A workspace tool that leaves a mark when it is imported, i.e. when its code runs.
EVIL_TOOL = '''
from pathlib import Path
Path({marker!r}).write_text("ran")
NAME = "safe_evil"
DESCRIPTION = "x"
def run(args, ctx):
    return "x"
'''


class WorkspaceTrust(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.ws = root / "ws"
        (self.ws / ".smallharness" / "tools").mkdir(parents=True)
        self.marker = root / "imported"
        self.tool = self.ws / ".smallharness" / "tools" / "safe_evil.py"
        self.tool.write_text(EVIL_TOOL.format(marker=str(self.marker)))
        (self.ws / "smallharness.toml").write_text(
            '[litert.cli]\ncommand = ["./evil.sh", "{prompt}"]\n[litert.server]\nport = 9400\n')
        env = mock.patch.dict(os.environ, {"SMALLHARNESS_TRUST_FILE": str(root / "trusted.json")})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def harness(self, cfg):
        cfg.data["model"]["backend"] = "mock"
        return Harness(cfg, backend_factory=MockBackend)

    def test_untrusted_code_is_withheld(self):
        cfg = Config.load(self.ws)
        self.assertEqual(cfg["litert"]["cli"]["command"], DEFAULTS["litert"]["cli"]["command"])
        self.assertEqual(cfg["litert"]["server"]["port"], 9400)        # harmless settings still apply
        self.assertEqual(list(cfg.untrusted.settings), ["litert.cli.command"])
        self.assertEqual([f.name for f in cfg.untrusted.tools], ["safe_evil.py"])
        h = self.harness(cfg)
        self.assertIsNone(h.tools.get("safe_evil"))
        self.assertFalse(self.marker.exists(), "an untrusted tool file was imported")
        self.assertTrue(any("not trusted" in w for w in h.tools.warnings))

    def test_trusting_uses_the_code_and_a_change_asks_again(self):
        cfg = Config.load(self.ws)
        cfg.grant_trust()
        self.assertIsNone(cfg.untrusted)
        self.assertEqual(cfg["litert"]["cli"]["command"], ["./evil.sh", "{prompt}"])
        self.assertIsNotNone(self.harness(cfg).tools.get("safe_evil"))
        self.assertTrue(self.marker.exists())
        self.assertIsNone(Config.load(self.ws).untrusted)              # remembered

        self.marker.unlink()
        self.tool.write_text(self.tool.read_text() + "\n# changed\n")  # e.g. after a git pull
        cfg = Config.load(self.ws)
        self.assertIsNotNone(cfg.untrusted)
        self.assertEqual(cfg["litert"]["cli"]["command"], DEFAULTS["litert"]["cli"]["command"])
        self.assertIsNone(self.harness(cfg).tools.get("safe_evil"))
        self.assertFalse(self.marker.exists())

    def test_tool_added_after_start_is_not_loaded_on_reload(self):
        cfg = Config.load(self.ws)
        cfg.grant_trust()
        h = self.harness(cfg)
        (self.ws / ".smallharness" / "tools" / "safe_new.py").write_text(
            'NAME = "safe_new"\nDESCRIPTION = "x"\ndef run(a, c): return "x"\n')
        self.assertFalse(cfg.workspace_trusted())
        self.assertIsNone(h._load_tools().get("safe_new"))

    def test_trust_for_one_run_is_not_saved(self):
        cfg = Config.load(self.ws, trust_run=True)
        self.assertEqual(cfg["litert"]["cli"]["command"], ["./evil.sh", "{prompt}"])
        self.assertIsNotNone(self.harness(cfg).tools.get("safe_evil"))
        self.assertIsNotNone(Config.load(self.ws).untrusted)

    def test_nothing_to_trust(self):
        self.tool.unlink()
        (self.ws / "smallharness.toml").write_text(
            f'[litert.cli]\ncommand = {DEFAULTS["litert"]["cli"]["command"]!r}\n'.replace("'", '"'))
        self.assertIsNone(Config.load(self.ws).untrusted)

    def test_explicit_config_file_is_the_users_own(self):
        own = Path(self.tmp.name) / "mine.toml"
        own.write_text('[rag.ocr]\nimage = ["my-ocr", "{input}"]\n')
        cfg = Config.load(self.ws, explicit=own)
        self.assertEqual(cfg["rag"]["ocr"]["image"], ["my-ocr", "{input}"])
        self.assertEqual(cfg.untrusted.settings, {})                    # the workspace file is not read
        self.assertTrue(cfg.untrusted.tools)                            # its tool files still need trust

    def test_prompt(self):
        cfg = Config.load(self.ws)
        with redirect_stdout(io.StringIO()) as out:
            cli.confirm_trust(cfg, interactive=False)
        self.assertIn("litert.cli.command", out.getvalue())
        self.assertIsNotNone(cfg.untrusted)
        with redirect_stdout(io.StringIO()), mock.patch("builtins.input", return_value="y"):
            cli.confirm_trust(cfg, interactive=True)
        self.assertIsNone(cfg.untrusted)
        self.assertEqual(cfg["litert"]["cli"]["command"], ["./evil.sh", "{prompt}"])


# Stands in for `litert-lm serve`; writes its pid so the test can check it is gone.
FAKE_SERVE = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(sys.argv[sys.argv.index("--port") + 1])
open(sys.argv[sys.argv.index("--pidfile") + 1], "w").write(str(os.getpid()))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
    def do_GET(self): self.reply({"data": [{"id": "m"}]})
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.reply({"choices": [{"message": {"content": "ok"}}]})
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ServerCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.ws, self.home = root / "ws", root / "home"
        self.ws.mkdir()
        self.home.mkdir()
        self.pidfile = root / "server.pid"
        script = root / "fake_serve.py"
        script.write_text(FAKE_SERVE)
        cmd = [sys.executable, str(script), "--port", "{port}", "--pidfile", str(self.pidfile)]
        self.config = root / "own.toml"   # the user's own file (--config): trusted
        self.config.write_text(
            "[model]\nbackend = \"litert\"\ncontext_tokens = 2048\n"
            "[litert.server]\nlaunch = true\nstartup_timeout = 20\n"
            f"command = [{', '.join(repr(c).replace(chr(39), chr(34)) for c in cmd)}]\n")
        env = mock.patch.dict(os.environ, {"SMALLHARNESS_TRUST_FILE": str(root / "trusted.json")})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        if self.pidfile.exists() and _alive(int(self.pidfile.read_text())):
            os.kill(int(self.pidfile.read_text()), 9)
        self.tmp.cleanup()

    def server_pid(self) -> int:
        self.assertTrue(self.pidfile.exists(), "the server was not started")
        return int(self.pidfile.read_text())

    def test_check_probe_stops_the_server(self):
        procs = []
        load = ManagedServerBackend.load

        def spy(be):
            load(be)
            procs.append((be, be.proc))
        with mock.patch.object(ManagedServerBackend, "load", spy), redirect_stdout(io.StringIO()) as out:
            code = cli.main(["-w", str(self.ws), "-c", str(self.config), "--no-color", "check", "--probe"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(len(procs), 1)
        be, proc = procs[0]
        self.assertIsNotNone(proc.poll(), "check --probe left the model server running")
        self.assertNotIn(be, server._LIVE)

    def test_leftover_servers_are_stopped_at_exit(self):
        cfg = Config.load(self.ws, explicit=self.config)
        be = ManagedServerBackend(cfg["litert"]["server"])
        be.load()
        proc = be.proc
        self.assertIn(be, server._LIVE)
        server._stop_leftover_servers()          # what atexit runs
        self.assertIsNotNone(proc.poll())
        self.assertNotIn(be, server._LIVE)

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sigterm_stops_the_server(self):
        env = {**os.environ, "HOME": str(self.home), "PYTHONPATH": str(REPO)}
        harness = subprocess.Popen(
            [sys.executable, "-m", "smallharness", "-w", str(self.ws), "-c", str(self.config), "--no-color"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            deadline = time.time() + 20
            while not self.pidfile.exists() and time.time() < deadline:
                time.sleep(0.1)
            pid = self.server_pid()
            time.sleep(1.0)                       # let the harness reach its prompt
            harness.terminate()                   # SIGTERM
            harness.wait(20)
        finally:
            harness.stdin.close()
            if harness.poll() is None:
                harness.kill()
        deadline = time.time() + 5
        while _alive(pid) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(_alive(pid), "the model server outlived the harness")


if __name__ == "__main__":
    unittest.main()
