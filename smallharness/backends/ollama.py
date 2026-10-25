"""Ollama as the engine, through its own API (/api/chat), not the OpenAI-compatible
one: only the native API takes the context size (`num_ctx`) per request. Without it
Ollama uses its small default and silently cuts longer prompts, while the harness
would fill the window it believes it has.

Ollama keeps its own models (GGUF, pulled by tag such as `qwen3:0.6b`); the
.litertlm files in the model folder are not used here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlparse

from ..jsonhttp import HttpError, open_json
from .base import Backend, apply_stop

DEFAULT_URL = "http://127.0.0.1:11434"
_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.S)


def ollama_model(cfg) -> str:
    """[ollama].model, else [model].path when it is a name rather than a file."""
    name = cfg["ollama"].get("model", "")
    path = cfg["model"]["path"]
    return name or (path if path and not Path(path).expanduser().is_file() else "")


def is_local(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "") in ("127.0.0.1", "localhost", "::1")


def matches(want: str, have: str) -> bool:
    """`qwen3` is listed by Ollama as `qwen3:latest`."""
    return have == want or (":" not in want and have == want + ":latest")


@dataclass
class OllamaModel:
    name: str
    size: int                      # bytes on disk
    parameters: str = ""           # e.g. "751.63M"
    quantization: str = ""
    max_context: int | None = None


class OllamaError(RuntimeError):
    pass


class Client:
    def __init__(self, cfg: dict):
        self.base = (cfg.get("base_url") or DEFAULT_URL).rstrip("/")
        self.timeout = cfg.get("timeout", 600)

    def request(self, path: str, payload: dict | None = None, method: str = "",
                timeout: float | None = None):
        try:
            return open_json(self.base, path, payload, method=method, timeout=timeout or self.timeout)
        except HttpError as e:
            if e.status is None:
                raise OllamaError(f"cannot reach Ollama at {self.base} ({e.detail}); "
                                  "is it running (`ollama serve`)?") from None
            try:
                msg = json.loads(e.detail).get("error") or e.detail
            except (ValueError, AttributeError):
                msg = e.detail
            if e.status == 404 and "not found" in str(msg):
                msg = f"{msg} (pull it with `smallharness -b ollama pull NAME` or `ollama pull NAME`)"
            raise OllamaError(f"Ollama: {str(msg)[:400]}") from None

    def get(self, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        with self.request(path, payload, timeout=timeout) as r:
            return json.load(r)

    def names(self, timeout: float | None = None) -> list[str]:
        return [m.get("name", "") for m in self.get("/api/tags", timeout=timeout).get("models", [])]

    def show(self, model: str, timeout: float | None = None) -> OllamaModel:
        data = self.get("/api/show", {"model": model}, timeout=timeout)
        info, details = data.get("model_info") or {}, data.get("details") or {}
        ctx = next((v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int)), None)
        size = next((m.get("size", 0) for m in self.get("/api/tags", timeout=timeout).get("models", [])
                     if matches(model, m.get("name", ""))), 0)
        return OllamaModel(model, size, details.get("parameter_size", ""),
                           details.get("quantization_level", ""), ctx)

    def list(self) -> list[OllamaModel]:
        out = []
        for m in self.get("/api/tags").get("models", []):
            details = m.get("details") or {}
            om = OllamaModel(m.get("name", ""), m.get("size", 0), details.get("parameter_size", ""),
                             details.get("quantization_level", ""))
            try:
                om.max_context = self.show(om.name).max_context
            except OllamaError:
                pass
            out.append(om)
        return out

    def pull(self, model: str, progress: Callable[[int, int | None], None] | None = None) -> None:
        """Download a model into Ollama's store. Ollama resumes interrupted pulls itself."""
        with self.request("/api/pull", {"model": model, "stream": True}) as r:
            for raw in r:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("error"):
                    raise OllamaError(f"Ollama: {event['error']}")
                if progress and event.get("total"):
                    progress(event.get("completed", 0), event["total"])
                if event.get("status") == "success":
                    return
        raise OllamaError("Ollama ended the pull without reporting success; run it again")

    def delete(self, model: str) -> None:
        self.request("/api/delete", {"model": model}, method="DELETE").close()


def model_facts(ocfg: dict, model: str) -> OllamaModel | None:
    """The model's maximum context and size, or None if Ollama cannot say right now."""
    if not model:
        return None
    try:
        return Client(ocfg).show(model, timeout=10)
    except OllamaError:
        return None


class OllamaBackend(Backend):
    name = "ollama"
    mode = "ollama"
    memory = "external"

    def __init__(self, cfg: dict, model: str, context_tokens: int):
        self.cfg = cfg
        self.client = Client(cfg)
        self.base = self.client.base
        self.model = model
        self.context_tokens = context_tokens
        self.max_concurrent = max(1, int(cfg.get("max_concurrent", 1)))

    def load(self) -> None:
        """Check Ollama is up and has the model; pick the first one if none is set."""
        names = self.client.names()
        if not self.model:
            if not names:
                raise OllamaError("Ollama has no models: pull one with `smallharness -b ollama pull NAME`")
            self.model = names[0]
        elif not any(matches(self.model, n) for n in names):
            have = ", ".join(names) or "none"
            raise OllamaError(f"Ollama has no model {self.model!r} (it has: {have}); "
                              f"pull it with `smallharness -b ollama pull {self.model}`")

    def _payload(self, prompt: str, max_tokens: int, stop: list[str] | None, stream: bool) -> dict:
        options = {"num_ctx": self.context_tokens, "num_predict": max_tokens}
        if stop:
            options["stop"] = stop
        p = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
             "stream": stream, "options": options}
        if self.cfg.get("keep_alive"):
            p["keep_alive"] = self.cfg["keep_alive"]
        if isinstance(self.cfg.get("think"), bool):
            p["think"] = self.cfg["think"]
        return p

    # Ollama's prompt_eval_count leaves out the part of the prompt it had cached from the
    # previous call, so it is not used to calibrate the token estimate.

    def generate(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> str:
        data = self.client.get("/api/chat", self._payload(prompt, max_tokens, stop, False))
        if data.get("error"):
            raise OllamaError(f"Ollama: {data['error']}")
        try:
            text = data["message"].get("content") or ""
        except (KeyError, AttributeError):
            raise OllamaError(f"unexpected Ollama response: {str(data)[:300]}") from None
        return apply_stop(_THINK.sub("", text, count=1), stop)

    def stream(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> Iterator[str]:
        text, sent, thinking = "", 0, None
        with self.client.request("/api/chat", self._payload(prompt, max_tokens, stop, True)) as r:
            for raw in r:  # one JSON object per line, the last with "done": true
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if event.get("error"):
                    raise OllamaError(f"Ollama: {event['error']}")
                text += (event.get("message") or {}).get("content") or ""
                if thinking is None:
                    head = text.lstrip()
                    if "<think>".startswith(head[:7]) and not event.get("done"):
                        if "</think>" not in head:
                            continue                       # maybe reasoning: hold back until it ends
                        thinking = True
                    else:
                        thinking = head.startswith("<think>") and "</think>" in head
                body = text[text.find("</think>") + 8:].lstrip() if thinking else text
                cut = apply_stop(body, stop)
                if len(cut) > sent:
                    yield cut[sent:]
                    sent = len(cut)
                if len(cut) < len(body) or event.get("done"):
                    break
