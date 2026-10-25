import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from smallharness import pool as pool_mod
from smallharness.backends import make_backend
from smallharness.backends.base import Backend
from smallharness.backends.litert import LiteRTBackend, extract_output, resolve_mode
from smallharness.backends.server import ManagedServerBackend, ServerBackend
from smallharness.config import Config
from smallharness.jsonhttp import normalize_base_url
from smallharness.pool import ModelPool
from smallharness.resources import CapacityReport, RamInfo

# A stand-in for a LiteRT-LM command: noisy logs and colors around the answer.
FAKE_CLI = r'''
import sys
mode = sys.argv[1]
if mode == "--stdin":
    prompt = sys.stdin.read()
elif mode == "--file":
    prompt = open(sys.argv[2]).read()
    print("prompt file:", sys.argv[2])
else:
    prompt = sys.argv[2]
print("\x1b[32mINFO\x1b[0m loading model... done in 2.1s")
print("ANSWER: echo " + prompt.strip())
print("stats: 12 tokens/s", file=sys.stderr)
'''


class CliMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / "fake_litert.py"
        self.script.write_text(FAKE_CLI)

    def tearDown(self):
        self.tmp.cleanup()

    def backend(self, via, args, regex=r"ANSWER: (.*)"):
        cfg = {"cli": {"command": [sys.executable, str(self.script), *args],
                       "prompt_via": via, "output_regex": regex, "timeout": 30}}
        be = LiteRTBackend("", cfg, "cli")
        be.load()
        return be

    def test_stdin(self):
        self.assertEqual(self.backend("stdin", ["--stdin"]).generate("hello", 10), "echo hello")

    def test_private_temp_file_is_removed(self):
        be = self.backend("file", ["--file", "{prompt_file}"])
        proc = be.run_command("hi there", 10)
        path = proc.stdout.split("prompt file: ")[1].splitlines()[0]
        self.assertFalse(Path(path).exists())
        self.assertIn("echo hi there", proc.stdout)

    def test_arg_and_length_limit(self):
        be = self.backend("arg", ["--arg", "{prompt}"])
        self.assertEqual(be.generate("short", 10), "echo short")
        with self.assertRaisesRegex(RuntimeError, "too long"):
            be.generate("x" * 200_000, 10)

    def test_output_cleanup(self):
        self.assertEqual(extract_output("\x1b[1mhi\x1b[0m\r\n"), "hi")
        with self.assertRaisesRegex(RuntimeError, "did not match"):
            extract_output("no marker", r"ANSWER: (.*)")

    def test_config_validation(self):
        with self.assertRaisesRegex(RuntimeError, "needs"):
            LiteRTBackend("", {"cli": {"command": []}}, "cli")
        with self.assertRaisesRegex(RuntimeError, "prompt_file"):
            LiteRTBackend("", {"cli": {"command": ["x"], "prompt_via": "file"}}, "cli")
        with self.assertRaisesRegex(RuntimeError, "model"):
            LiteRTBackend("", {"cli": {"command": ["x", "{model}"]}}, "cli")

    def test_never_falls_back_to_cli(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "no LiteRT-LM runtime"):
                resolve_mode({"mode": "auto", "server": {"base_url": ""}})
        self.assertEqual(resolve_mode({"mode": "auto", "server": {"base_url": "http://x"}}), "server")
        self.assertEqual(resolve_mode({"mode": "cli"}), "cli")


class FakeServer(BaseHTTPRequestHandler):
    requests: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._json({"data": [{"id": "gemma-test"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeServer.requests.append(body)
        if not body["stream"]:
            return self._json({"choices": [{"message": {"content": "Hello there"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in ["Hel", "lo", "\nUSER: leaked", " more"]:
            chunk = {"choices": [{"delta": {"content": piece}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ServerMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_generate_and_payload(self):
        be = ServerBackend({"base_url": self.url})
        be.load()
        self.assertEqual(be.model, "gemma-test")
        self.assertEqual(be.generate("prompt text", 50, ["\nUSER:"]), "Hello there")
        sent = FakeServer.requests[-1]
        self.assertEqual(sent["max_tokens"], 50)
        self.assertEqual(sent["messages"], [{"role": "user", "content": "prompt text"}])

    def test_stream_stops_at_stop_sequence(self):
        be = ServerBackend({"base_url": self.url + "/v1"})
        be.load()
        self.assertEqual("".join(be.stream("p", 50, ["\nUSER:"])), "Hello")

    def test_errors(self):
        with self.assertRaisesRegex(RuntimeError, "no model 'nope'"):
            ServerBackend({"base_url": self.url, "model": "nope"}).load()
        with self.assertRaisesRegex(RuntimeError, "cannot reach"):
            ServerBackend({"base_url": "http://127.0.0.1:9", "timeout": 2}).load()

    def test_auto_mode_picks_server(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config.load(Path(d), overrides={"litert": {"server": {"base_url": self.url}}})
            self.assertIsInstance(make_backend(cfg), ServerBackend)

    def test_url_normalization(self):
        self.assertEqual(normalize_base_url("http://h:8080/"), "http://h:8080/v1")
        self.assertEqual(normalize_base_url("http://h:8080/api/v1"), "http://h:8080/api/v1")


class SlowBackend(Backend):
    def __init__(self, memory):
        self.memory = memory
        self.max_concurrent = 2

    def generate(self, prompt, max_tokens, stop=None):
        return ""


def fake_report(max_instances, fits=True):
    return CapacityReport(RamInfo(8000, 6000, "test"), 2000, 2500, 5000, max_instances, fits)


class PoolAccounting(unittest.TestCase):
    def pool(self, memory):
        cfg = Config(Path(tempfile.gettempdir()))
        cfg.data["model"]["path"] = "/models/m.litertlm"
        return ModelPool(cfg, lambda: SlowBackend(memory))

    def peak_concurrency(self, p, workers=6):
        active, peak, lock = [0], [0], threading.Lock()

        def work(_):
            with p.acquire():
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with lock:
                    active[0] -= 1

        threads = [threading.Thread(target=work, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return peak[0]

    def test_per_call_does_not_count_slots_as_loaded(self):
        seen = []

        def cap(path, ctx, res, already_loaded=0):
            seen.append(already_loaded)
            return fake_report(2)

        with mock.patch.object(pool_mod, "capacity", cap):
            p = self.pool("per_call")
            p.spawn(5)
            self.assertEqual(len(p.instances), 2)
            self.assertEqual(set(seen), {0})
            self.assertLessEqual(self.peak_concurrency(p), 2)

    def test_per_call_limit_measured_only_when_idle(self):
        with mock.patch.object(pool_mod, "capacity", lambda *a, **k: fake_report(3)):
            p = self.pool("per_call")
            p.spawn(3)
            with p.acquire():
                with mock.patch.object(pool_mod, "capacity", lambda *a, **k: fake_report(1)):
                    self.assertEqual(p.capacity, 3)  # mid-call reading is ignored
            with mock.patch.object(pool_mod, "capacity", lambda *a, **k: fake_report(1)):
                self.assertEqual(p.capacity, 1)      # idle: re-measured
            with mock.patch.object(pool_mod, "capacity", lambda *a, **k: fake_report(0, fits=False)):
                with self.assertRaisesRegex(RuntimeError, "not enough free RAM"):
                    with p.acquire():
                        pass

    def test_resident_counts_loaded_instances(self):
        seen = []

        def cap(path, ctx, res, already_loaded=0):
            seen.append(already_loaded)
            return fake_report(2)

        with mock.patch.object(pool_mod, "capacity", cap):
            p = self.pool("resident")
            p.spawn(5)
            self.assertEqual(len(p.instances), 2)
            self.assertIn(1, seen)

    def test_external_uses_server_concurrency(self):
        p = self.pool("external")
        p.spawn(5)
        self.assertEqual(p.capacity, 2)
        self.assertLessEqual(self.peak_concurrency(p), 2)


# Stands in for `litert-lm serve --host 127.0.0.1 --port N`.
FAKE_SERVE = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
if "--crash" in sys.argv:
    print("error: model cache is corrupt"); sys.exit(3)
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)
    def do_GET(self): self.reply({"data": [{"id": "gemma-4-E2B-it"}]})
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.reply({"choices": [{"message": {"content": f"served on {port}"}}]})
print("listening", flush=True)
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


class ManagedServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / "fake_serve.py"
        self.script.write_text(FAKE_SERVE)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, *extra, port=None):
        port = port or _free_port()
        return {"command": [sys.executable, str(self.script), "--host", "127.0.0.1", "--port", "{port}", *extra],
                "port": port, "startup_timeout": 20, "launch": True}

    def test_launch_generate_close(self):
        be = ManagedServerBackend(self.cfg())
        try:
            be.load()
            self.assertEqual(be.model, "gemma-4-E2B-it")
            self.assertTrue(be.generate("hi", 10).startswith("served on"))
            proc = be.proc
        finally:
            be.close()
        self.assertIsNotNone(proc.poll())  # process stopped

    def test_busy_port_moves_to_free_one(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            busy = s.getsockname()[1]
            be = ManagedServerBackend(self.cfg(port=busy))
            try:
                be.load()
                self.assertNotIn(f":{busy}/", be.base)
            finally:
                be.close()

    def test_crash_reports_log(self):
        be = ManagedServerBackend(self.cfg("--crash"))
        with self.assertRaisesRegex(RuntimeError, "model cache is corrupt"):
            be.load()

    def test_auto_mode_and_pool_counting(self):
        self.assertEqual(resolve_mode({"mode": "auto", "server": {"launch": True}}), "server")
        with tempfile.TemporaryDirectory() as d:
            cfg = Config.load(Path(d), overrides={"litert": {"server": self.cfg()}})
            be = make_backend(cfg)
            self.assertIsInstance(be, ManagedServerBackend)
            self.assertEqual(be.memory, "resident")
            p = ModelPool(cfg, lambda: make_backend(cfg))
            self.assertIn("Model size unknown", p.report().describe())  # no [model].path
            self.assertEqual(p.capacity, 1)


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]




class UnmanagedRam(unittest.TestCase):
    def pool(self, memory, **res):
        cfg = Config(Path(tempfile.gettempdir()))
        cfg.data["model"]["path"] = "/models/m.litertlm"
        cfg.data["resources"].update(manage_ram=False, **res)
        return ModelPool(cfg, lambda: SlowBackend(memory))

    def test_one_instance_and_no_refusal(self):
        for memory in ("resident", "per_call", "external"):
            with mock.patch.object(pool_mod, "capacity", lambda *a, **k: fake_report(0, fits=False)):
                p = self.pool(memory)
                rep = p.report()
                self.assertTrue(rep.fits)
                self.assertIn("does not fit", rep.note)
                self.assertEqual(p.spawn(4), 1)          # capped at one
                with p.acquire():                        # no RAM refusal, even per_call
                    pass
                self.assertEqual(p.capacity, 1)

    def test_unreadable_ram_is_not_an_error(self):
        def broken(*a, **k):
            raise RuntimeError("cannot read RAM")
        with mock.patch.object(pool_mod, "capacity", broken):
            p = self.pool("resident")
            self.assertIn("disabled", p.report().describe())
            p.ensure_one()
            self.assertEqual(len(p.instances), 1)




class JsonHttp(unittest.TestCase):
    def test_failures_become_one_error(self):
        import http.server
        import threading

        from smallharness.jsonhttp import HttpError, call_json

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"nope" if self.path == "/bad" else b"not json"
                self.send_response(500 if self.path == "/bad" else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with self.assertRaises(HttpError) as e:
            call_json(base, "/bad")
        self.assertEqual((e.exception.status, e.exception.detail), (500, "nope"))
        with self.assertRaises(HttpError) as e:
            call_json(base, "/text")
        self.assertEqual(e.exception.status, 200)
        with self.assertRaises(HttpError) as e:
            call_json("http://127.0.0.1:9", "/x", timeout=2)                  # nothing listens there
        self.assertIsNone(e.exception.status)


if __name__ == "__main__":
    unittest.main()
