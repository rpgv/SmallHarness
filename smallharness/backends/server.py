"""Client for an OpenAI-compatible chat completions server, such as `litert-lm serve`
(endpoints /v1/models, /v1/chat/completions, /v1/embeddings).

The server keeps the model loaded, so there is no per-call reload, output can be
streamed, the prompt travels in the request body, and max_tokens is enforced by
the server. The harness still owns the context: each call sends the fully
assembled window as one user message."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Iterator

from ..jsonhttp import HttpError, normalize_base_url, open_json
from .base import Backend, apply_stop


class ServerBackend(Backend):
    name = "litert"
    mode = "server"
    memory = "external"

    def __init__(self, cfg: dict, model: str = ""):
        if not cfg.get("base_url"):
            raise RuntimeError("server mode needs [litert.server].base_url, e.g. http://127.0.0.1:8080")
        self.base = normalize_base_url(cfg["base_url"])
        self.model = cfg.get("model") or model
        self.api_key = cfg.get("api_key", "")
        self.timeout = cfg.get("timeout", 600)
        self.max_concurrent = max(1, int(cfg.get("max_concurrent", 1)))

    # ---------------------------------------------------------------- http
    def _request(self, path: str, payload: dict | None = None):
        try:
            return open_json(self.base, path, payload, api_key=self.api_key, timeout=self.timeout)
        except HttpError as e:
            if e.status is None:
                raise RuntimeError(f"cannot reach {self.base} ({e.detail}); is `litert-lm serve` running?") from None
            raise RuntimeError(f"server returned {e.status} for {path}: {e.detail[:500]}") from None

    def load(self) -> None:
        """Check the server is up and pick a model if none is configured."""
        try:
            with self._request("/models") as r:
                models = [m.get("id") for m in json.load(r).get("data", [])]
        except RuntimeError as e:
            if "cannot reach" in str(e):
                raise
            models = []  # /models not implemented; the first completion will tell
        if not self.model:
            if not models:
                raise RuntimeError("the server lists no models: import one with `litert-lm import`, "
                                   "or set [litert.server].model")
            self.model = models[0]
        elif models and self.model not in models:
            raise RuntimeError(f"server has no model {self.model!r}; it has: {', '.join(models)}. "
                               "Import it with `litert-lm import` (see `litert-lm list`)")

    def _payload(self, prompt: str, max_tokens: int, stop: list[str] | None, stream: bool) -> dict:
        p = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
             "max_tokens": max_tokens, "stream": stream}
        if stop:
            p["stop"] = stop[:4]  # the OpenAI API allows at most 4
        return p

    last_prompt_tokens: int | None = None

    def _note_usage(self, data) -> None:
        """OpenAI-style replies report the real prompt size; the harness uses it to
        calibrate its token estimate."""
        try:
            n = data["usage"]["prompt_tokens"]
        except (KeyError, TypeError):
            return
        if isinstance(n, int) and n > 0:
            self.last_prompt_tokens = n

    # ---------------------------------------------------------- generation
    def generate(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> str:
        with self._request("/chat/completions", self._payload(prompt, max_tokens, stop, False)) as r:
            data = json.load(r)
        self._note_usage(data)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected server response: {str(data)[:300]}") from None
        return apply_stop(text, stop)

    def stream(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> Iterator[str]:
        text, sent = "", 0
        with self._request("/chat/completions", self._payload(prompt, max_tokens, stop, True)) as r:
            for raw in r:  # server-sent events: "data: {...}" lines, ending with "data: [DONE]"
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    event = json.loads(body)
                    self._note_usage(event)  # some servers send usage in the last event
                    delta = event["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                text += delta.get("content") or ""
                cut = apply_stop(text, stop)
                if len(cut) > sent:
                    yield cut[sent:]
                    sent = len(cut)
                if len(cut) < len(text):  # stop sequence reached
                    break


# Servers started by the harness and not yet stopped. They run in their own session
# (so Ctrl-C in the terminal does not hit them) and would outlive the harness if it
# exited without close(); this hook stops any that are left.
_LIVE: set["ManagedServerBackend"] = set()


@atexit.register
def _stop_leftover_servers() -> None:
    for backend in list(_LIVE):
        backend.close()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _any_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ManagedServerBackend(ServerBackend):
    """Starts its own `litert-lm serve` process and stops it on close. Each instance is
    one server process, so the pool counts it as resident RAM like an in-process model.

    The command binds to 127.0.0.1: `litert-lm serve` defaults to 0.0.0.0, which would
    expose the model to the network."""
    memory = "resident"

    def __init__(self, cfg: dict, model: str = ""):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.log_path = ""
        super().__init__({**cfg, "base_url": "http://127.0.0.1:0"}, model)
        self.max_concurrent = max(1, int(cfg.get("max_concurrent", 1)))

    def load(self) -> None:
        cmd = self.cfg["command"]
        if not shutil.which(cmd[0]):
            raise RuntimeError(f"`{cmd[0]}` is not on PATH ([litert.server].command)")
        port = self.cfg.get("port", 9379)
        if not _port_free(port):
            port = _any_free_port()
        argv = [a.format(port=port) for a in cmd]
        fd, self.log_path = tempfile.mkstemp(prefix="smallharness-serve-", suffix=".log")
        with os.fdopen(fd, "w") as log:
            self.proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
        _LIVE.add(self)
        self.base = f"http://127.0.0.1:{port}/v1"
        deadline = time.time() + self.cfg.get("startup_timeout", 120)
        while True:
            if self.proc.poll() is not None:
                msg = f"`{' '.join(argv)}` exited with {self.proc.returncode}:\n{self._log_tail()}"
                self.close()  # forget the process and remove its log
                raise RuntimeError(msg)
            try:
                super().load()
                return
            except RuntimeError as e:
                if "cannot reach" not in str(e):
                    self.close()
                    raise
            if time.time() > deadline:
                self.close()
                raise RuntimeError(f"server did not answer on port {port} within "
                                   f"{self.cfg.get('startup_timeout', 120)}s:\n{self._log_tail()}")
            time.sleep(0.3)

    def _log_tail(self, n: int = 15) -> str:
        try:
            with open(self.log_path, errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return ""

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        _LIVE.discard(self)
        if self.log_path and os.path.exists(self.log_path):
            os.unlink(self.log_path)
            self.log_path = ""
