"""The Ollama backend and commands, against a stand-in for Ollama's API."""
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from smallharness import cli
from smallharness.agent import Harness
from smallharness.backends import make_backend
from smallharness.backends.ollama import OllamaBackend, OllamaError
from smallharness.config import Config
from smallharness.engines import engine


class FakeOllama:
    """Has qwen3:latest (40960 ctx) and tiny:1b (no context_length reported).
    Chat replies are `reply`, streamed a few characters per line."""

    def __init__(self):
        self.models = {"qwen3:latest": 40960, "tiny:1b": None}
        self.reply = "Hello there. STOP ignored"
        self.calls: list[dict] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _lines(self, events):
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for e in events:
                    self.wfile.write(json.dumps(e).encode() + b"\n")

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                if self.path == "/api/tags":
                    self._json({"models": [{"name": n, "size": 500_000_000,
                                            "details": {"parameter_size": "751M", "quantization_level": "Q4_K_M"}}
                                           for n in fake.models]})
                else:
                    self._json({"error": "not found"}, 404)

            def do_DELETE(self):
                body = self._body()
                if fake.models.pop(body.get("model"), "gone") == "gone":
                    self._json({"error": f"model '{body.get('model')}' not found"}, 404)
                else:
                    self._json({})

            def do_POST(self):
                body = self._body()
                fake.calls.append({"path": self.path, **body})
                name = body.get("model", "")
                known = name in fake.models or name + ":latest" in fake.models
                if self.path == "/api/show":
                    if not known:
                        return self._json({"error": f"model '{name}' not found"}, 404)
                    ctx = fake.models.get(name, fake.models.get(name + ":latest"))
                    info = {"general.architecture": "qwen3"} | ({"qwen3.context_length": ctx} if ctx else {})
                    return self._json({"model_info": info, "details": {"parameter_size": "751M"}})
                if self.path == "/api/pull":
                    if name == "missing":
                        return self._lines([{"status": "pulling manifest"},
                                            {"error": "pull model manifest: file does not exist"}])
                    fake.models[name] = 2048
                    return self._lines([{"status": "pulling manifest"},
                                        {"status": "pulling a1", "total": 100, "completed": 50},
                                        {"status": "pulling a1", "total": 100, "completed": 100},
                                        {"status": "pulling b2", "total": 10, "completed": 10},
                                        {"status": "success"}])
                if self.path == "/api/chat":
                    if not known:
                        return self._json({"error": f"model '{name}' not found"}, 404)
                    text = fake.reply
                    for s in (body.get("options") or {}).get("stop") or []:   # as Ollama does
                        text = text.split(s)[0]
                    if not body.get("stream"):
                        return self._json({"message": {"role": "assistant", "content": text},
                                           "done": True, "prompt_eval_count": 3})
                    parts = [text[i:i + 4] for i in range(0, len(text), 4)]
                    return self._lines([{"message": {"content": p}, "done": False} for p in parts]
                                       + [{"message": {"content": ""}, "done": True}])
                self._json({"error": "not found"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chats(self):
        return [c for c in self.calls if c["path"] == "/api/chat"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOllama()
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()
        env = mock.patch.dict("os.environ", {"SMALLHARNESS_TRUST_FILE": str(self.ws / "trusted.json")})
        env.start()
        self.addCleanup(env.stop)

    def ocfg(self, **over):
        return {"base_url": self.fake.url, "timeout": 10, "keep_alive": "", "think": "", **over}

    def cfg(self, **model):
        return Config.load(self.ws, overrides={"model": {"backend": "ollama", **model},
                                               "ollama": {"base_url": self.fake.url},
                                               "resources": {"manage_ram": False}})


class Backend(Base):
    def backend(self, model="qwen3", **over):
        be = OllamaBackend(self.ocfg(**over), model, 8192)
        be.load()
        return be

    def test_generate_sends_the_window_size(self):
        be = self.backend(keep_alive="30m", think=False)
        self.assertEqual(be.generate("hi", 64, ["STOP"]), "Hello there. ")
        call = self.fake.chats()[-1]
        self.assertEqual(call["options"], {"num_ctx": 8192, "num_predict": 64, "stop": ["STOP"]})
        self.assertEqual((call["keep_alive"], call["think"]), ("30m", False))
        self.assertEqual(call["messages"], [{"role": "user", "content": "hi"}])

    def test_stream(self):
        be = self.backend()
        self.assertEqual("".join(be.stream("hi", 64, ["STOP"])), "Hello there. ")
        self.fake.reply = "plain answer"
        self.assertEqual("".join(be.stream("hi", 64)), "plain answer")

    def test_reasoning_is_left_out(self):
        be = self.backend()
        self.fake.reply = "<think>\nlet me see\n</think>\n\nThe answer."
        self.assertEqual(be.generate("q", 64), "The answer.")
        self.assertEqual("".join(be.stream("q", 64)), "The answer.")

    def test_load(self):
        self.assertEqual(self.backend("").model, "qwen3:latest")    # none set: the first listed
        with self.assertRaisesRegex(OllamaError, "no model 'nope'.*pull it"):
            self.backend("nope")
        with self.assertRaisesRegex(OllamaError, "cannot reach Ollama"):
            OllamaBackend({"base_url": "http://127.0.0.1:9", "timeout": 2}, "x", 4096).load()


class Settings(Base):
    def test_context_comes_from_ollama(self):
        cfg = self.cfg(path="qwen3")                 # -m NAME: the model name
        self.assertEqual(cfg.context_tokens, 8192)   # 40960, capped at auto_context_max
        self.assertIn("per Ollama", cfg.context.note)
        self.assertEqual(cfg.model_name, "qwen3")
        be = make_backend(cfg)
        self.assertEqual((be.name, be.model, be.context_tokens), ("ollama", "qwen3", 8192))
        cfg.data["ollama"]["model"] = "tiny:1b"      # does not report its maximum
        self.assertEqual(cfg.context_tokens, 4096)
        self.assertIn("Ollama does not report it", cfg.context.note)

    def test_unreachable_is_asked_again(self):
        cfg = self.cfg(path="qwen3")
        cfg.data["ollama"]["base_url"] = "http://127.0.0.1:9"
        self.assertIn("did not answer", cfg.context.note)
        cfg.data["ollama"]["base_url"] = self.fake.url
        self.assertEqual(cfg.context_tokens, 8192)

    def test_harness_runs_on_ollama(self):
        h = Harness(self.cfg(path="qwen3"))
        self.addCleanup(h.close)
        h.start()
        self.fake.reply = "Paris."
        self.assertEqual(h.generate("Capital of France?"), "Paris.")
        self.assertEqual(self.fake.chats()[-1]["options"]["num_ctx"], 8192)
        self.assertEqual(h._model_server_url(), self.fake.url)     # embeddings: Ollama's /v1

    def test_model_command_sets_the_ollama_name(self):
        h = Harness(self.cfg(path="qwen3"))
        self.addCleanup(h.close)
        values, _ = engine(h.cfg).switch_values(["tiny:1b", "ctx=2048"])
        self.assertEqual(values["ollama.model"], "tiny:1b")
        self.assertEqual(values["model.context_tokens"], 2048)


class Commands(Base):
    def main(self, *args):
        with redirect_stdout(io.StringIO()) as out:
            code = cli.main(["-w", str(self.ws), "-b", "ollama", "--no-color", "--trust", *args])
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        (self.ws / "smallharness.toml").write_text(f'[ollama]\nbase_url = "{self.fake.url}"\n')

    def test_models_pull_delete(self):
        code, out = self.main("models")
        self.assertIn("qwen3:latest", out)
        self.assertIn("40960 ctx", out)
        self.assertIn("Q4_K_M", out)
        code, out = self.main("pull", "gemma3:1b")
        self.assertEqual(code, 0, out)
        self.assertIn("gemma3:1b", self.fake.models)
        self.assertIn("-b ollama -m gemma3:1b", out)
        code, out = self.main("pull", "missing")
        self.assertEqual(code, 1)
        self.assertIn("file does not exist", out)
        self.assertEqual(self.main("delete", "gemma3:1b")[0], 1)     # not a tty: asks, so refuses
        code, out = self.main("delete", "gemma3:1b", "-y")
        self.assertEqual(code, 0, out)
        self.assertNotIn("gemma3:1b", self.fake.models)
        self.assertEqual(self.main("delete", "gemma3:1b", "-y")[0], 1)
        self.assertFalse((self.ws / ".smallharness").exists())

    def test_delete_refuses_the_model_in_use(self):
        cfg = self.cfg(path="qwen3")
        with redirect_stdout(io.StringIO()) as out:
            self.assertFalse(cli.delete_model(cfg, "qwen3", assume_yes=True, in_use=True))
        self.assertIn("in use", out.getvalue())
        self.assertIn("qwen3:latest", self.fake.models)

    def test_ask(self):
        self.fake.reply = "pong"
        (self.ws / "faq.md").write_text("ping: the server answers pong\n")
        self.assertEqual(self.main("-m", "qwen3", "index", "faq.md")[0], 0)
        code, out = self.main("-m", "qwen3", "ask", "ping")
        self.assertEqual(code, 0, out)
        self.assertIn("pong", out)


if __name__ == "__main__":
    unittest.main()
