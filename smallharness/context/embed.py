"""Embedding models for hybrid RAG search. Optional: with [rag.embed].backend = "none"
search is BM25 only.

  server     any OpenAI-compatible /v1/embeddings endpoint: `litert-lm serve` with an
             embedding model, Ollama, llama.cpp server, LM Studio. No extra packages.
  fastembed  in-process ONNX models (`pip install fastembed`), e.g. BAAI/bge-small-en-v1.5.

Vectors are normalized when made, so similarity is a dot product. They are stored in
the index file as base64 float32.
"""
from __future__ import annotations

import base64
import math
from array import array
from typing import Callable

try:  # optional; only speeds up similarity over large indexes
    import numpy as _np
except ImportError:
    _np = None


class EmbedError(RuntimeError):
    pass


# ----------------------------------------------------------------- vectors
def normalize(v) -> array:
    v = array("f", v)
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array("f", (x / n for x in v))


def pack(v: array) -> str:
    return base64.b64encode(v.tobytes()).decode("ascii")


def unpack(s: str) -> array:
    v = array("f")
    v.frombytes(base64.b64decode(s))
    return v


def similarities(query: array, vectors: list[array]) -> list[float]:
    return similarities_matrix(query, pack_matrix(vectors))


def pack_matrix(vectors: list[array]):
    """Vectors packed once for repeated searching: a numpy matrix when numpy is
    installed, else the list itself."""
    if _np is not None and vectors:
        return _np.frombuffer(b"".join(v.tobytes() for v in vectors), dtype=_np.float32).reshape(len(vectors), -1)
    return vectors


def similarities_matrix(query: array, matrix) -> list[float]:
    if matrix is None or len(matrix) == 0:
        return []
    if _np is not None and not isinstance(matrix, list):
        return (matrix @ _np.frombuffer(query.tobytes(), dtype=_np.float32)).tolist()
    return [sum(a * b for a, b in zip(query, v)) for v in matrix]


# --------------------------------------------------------------- embedders
class Embedder:
    """Turns texts into normalized vectors. `id` names the model: vectors made by a
    different model are not comparable, so an index records it and re-embeds on change."""
    id = "none"
    max_chars = 2000  # small embedding models read ~512 tokens
    default_floor = 0.3  # what even unrelated text scores is model-specific

    @property
    def min_similarity(self) -> float:
        """[rag.embed].min_similarity, or (0 = auto) a floor that suits this model."""
        return float(self.cfg.get("min_similarity") or self.default_floor)

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.batch = max(1, int(cfg.get("batch", 32)))

    def embed_documents(self, texts: list[str]) -> list[array]:
        out = []
        for i in range(0, len(texts), self.batch):
            part = [self.cfg.get("doc_prefix", "") + t[:self.max_chars] for t in texts[i:i + self.batch]]
            out += [normalize(v) for v in self._embed(part, query=False)]
        return out

    def embed_query(self, text: str) -> array:
        return normalize(self._embed([self.cfg.get("query_prefix", "") + text[:self.max_chars]], query=True)[0])

    def _embed(self, texts: list[str], query: bool) -> list[list[float]]:
        raise NotImplementedError


class ServerEmbedder(Embedder):
    def __init__(self, cfg: dict, base_url: Callable[[], str]):
        super().__init__(cfg)
        self._base_url = base_url
        self.model = cfg.get("model", "")
        self.id = f"server:{self.model or 'default'}"

    def _embed(self, texts, query):
        from ..jsonhttp import HttpError, call_json, normalize_base_url
        base = self._base_url()
        if not base:
            raise EmbedError("no embedding server: set [rag.embed].base_url "
                             "(or [litert.server].base_url to use the model server)")
        body = {"input": texts}
        if self.model:
            body["model"] = self.model
        try:
            data = call_json(normalize_base_url(base), "/embeddings", body, api_key=self.cfg.get("api_key", ""),
                             timeout=self.cfg.get("timeout", 60))
        except HttpError as e:
            if e.status is None:
                raise EmbedError(f"embedding server at {base} is unreachable: {e.detail}") from None
            raise EmbedError(f"embedding server returned {e.status}: {e.detail[:200]}") from None
        try:
            rows = sorted(data["data"], key=lambda d: d.get("index", 0))
            vectors = [row["embedding"] for row in rows]
        except (KeyError, TypeError):
            raise EmbedError("embedding server reply has no data[].embedding") from None
        if len(vectors) != len(texts):
            raise EmbedError(f"embedding server returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors


class FastEmbedder(Embedder):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.model_name = cfg.get("fastembed_model") or "BAAI/bge-small-en-v1.5"
        self.id = f"fastembed:{self.model_name}"
        self._model = None
        if "bge-" in self.model_name.lower():   # bge scores unrelated text 0.35-0.5
            self.default_floor = 0.5

    def _embed(self, texts, query):
        if self._model is None:  # loaded on first use, not at startup
            try:
                from fastembed import TextEmbedding
            except ImportError:
                raise EmbedError("fastembed is not installed: pip install fastembed") from None
            self._model = TextEmbedding(model_name=self.model_name)
        m = self._model
        fn = getattr(m, "query_embed" if query else "passage_embed", None) or m.embed
        return [list(map(float, v)) for v in fn(texts)]


def fastembed_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("fastembed") is not None


def make_embedder(cfg: dict, server_url: Callable[[], str] = lambda: "", default_model: str = "") -> Embedder | None:
    """`auto` (the default): fastembed in process if it is installed; else the model's
    own server (/v1/embeddings, with the engine's usual embedding model) if one is
    configured; else none, and search uses keywords only."""
    kind = cfg.get("backend", "auto")
    if kind == "none":
        return None
    if kind == "auto":
        if fastembed_installed():
            return FastEmbedder(cfg)
        if cfg.get("base_url") or server_url():
            kind, cfg = "server", {**cfg, "model": cfg.get("model") or default_model}
        else:
            return None
    if kind == "server":
        explicit = cfg.get("base_url", "")
        return ServerEmbedder(cfg, (lambda: explicit) if explicit else server_url)
    if kind == "fastembed":
        return FastEmbedder(cfg)
    raise ValueError(f"unknown [rag.embed].backend {kind!r}; use auto, none, server or fastembed")


def ram_mb(cfg: dict) -> int:
    """RAM to keep free for an in-process embedding model."""
    backend = cfg.get("backend", "auto")
    if backend != "fastembed" and not (backend == "auto" and fastembed_installed()):
        return 0
    return int(cfg.get("ram_mb") or 300)
