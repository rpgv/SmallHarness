from __future__ import annotations

from .base import Backend
from .mock import MockBackend


def server_model_id(cfg) -> str:
    """The model ID to ask `litert-lm serve` for: [litert.server].model, else (with
    [models].auto_import) the ID a model file is imported under. Empty: the first listed."""
    from pathlib import Path

    from ..models import server_id
    srv, path = cfg["litert"]["server"], cfg["model"]["path"]
    if srv.get("model"):
        return srv["model"]
    if cfg["models"].get("auto_import") and path and Path(path).is_file():
        return server_id(path)
    return ""


def make_backend(cfg) -> Backend:
    """Construct (not load) a backend. Construction is cheap; load() does the heavy work."""
    kind = cfg["model"]["backend"]
    if kind == "mock":
        return MockBackend()
    if kind == "litert":
        from .litert import LiteRTBackend, resolve_mode
        mode = resolve_mode(cfg["litert"])
        if mode == "server":
            from .server import ManagedServerBackend, ServerBackend
            srv = cfg["litert"]["server"]
            cls = ManagedServerBackend if srv.get("launch") else ServerBackend
            return cls(srv, server_model_id(cfg) or cfg["model"]["name"])
        return LiteRTBackend(cfg["model"]["path"], cfg["litert"], mode, cfg.context_tokens)
    if kind == "ollama":
        from .ollama import OllamaBackend, ollama_model
        return OllamaBackend(cfg["ollama"], ollama_model(cfg), cfg.context_tokens)
    raise ValueError(f"unknown backend {kind!r}")


__all__ = ["Backend", "MockBackend", "make_backend", "server_model_id"]
