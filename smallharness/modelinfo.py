"""What a .litertlm model file says about its context window.

A .litertlm file is a container: an 8-byte "LITERTLM" magic, a version, and a
flatbuffer header listing sections (offsets, a data type, key-value items). One
section is an `LlmMetadata` protobuf; others are TFLite models tagged with a
`model_type`, among them `tf_lite_prefill_decode`, the model that runs the LLM.

The context size comes from, in order:
  1. LlmMetadata.max_num_tokens (field 5), when the model sets it;
  2. the KV-cache tensors of the prefill/decode model: one of their dimensions is
     the number of token slots, which fixes the maximum sequence length.
The KV-cache tensors also give the exact RAM the cache takes per token.

Only the header and flatbuffer tables are read (through mmap), not the weights.
Anything unexpected returns None, and the harness falls back to its estimates.
"""
from __future__ import annotations

import functools
import mmap
import os
import re
import struct
from dataclasses import dataclass, field

MAGIC = b"LITERTLM"
_SECTION_TFLITE, _SECTION_METADATA = 3, 5
_KV_NAME = re.compile(r"kv_cache_([kv])_(\d+)")
# TFLite TensorType -> bytes per element
_DTYPE_BYTES = {0: 4, 1: 2, 2: 4, 3: 1, 4: 8, 6: 1, 7: 2, 9: 1, 10: 8, 16: 2, 17: 0.5}
_DTYPE_NAMES = {0: "float32", 1: "float16", 3: "uint8", 9: "int8", 16: "bfloat16", 17: "int4"}


@dataclass
class ModelInfo:
    max_context: int | None             # tokens the model supports
    source: str                         # where max_context came from: "metadata" | "kv-cache"
    kv_layers: list[tuple[float, int]] = field(default_factory=list)  # (bytes per slot, slots) per tensor
    kv_dtype: str = ""

    @property
    def kv_bytes_per_token(self) -> float:
        return sum(b for b, _ in self.kv_layers)

    def kv_mb(self, context_tokens: int) -> int | None:
        """RAM for the KV cache at this context size (layers with a shorter
        sliding window are capped at their own length)."""
        if not self.kv_layers:
            return None
        return int(sum(b * min(context_tokens, slots) for b, slots in self.kv_layers) / 2**20) + 1

    def describe(self) -> str:
        parts = [f"max context {self.max_context} tokens ({self.source})" if self.max_context
                 else "max context unknown"]
        if self.kv_layers:
            parts.append(f"KV cache {self.kv_bytes_per_token / 1024:.1f} KB/token "
                         f"({len(self.kv_layers)} tensors, {self.kv_dtype})")
        return ", ".join(parts)


def read_model_info(path: str) -> ModelInfo | None:
    try:
        st = os.stat(path)
    except (OSError, ValueError, TypeError):
        return None
    return _read_cached(os.path.abspath(path), st.st_size, st.st_mtime)


@functools.lru_cache(maxsize=16)
def _read_cached(path: str, size: int, mtime: float) -> ModelInfo | None:
    try:
        with open(path, "rb") as f:
            if f.read(8) != MAGIC:
                return None
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return _parse(_Buf(mm))
    except (OSError, ValueError, TypeError, struct.error, IndexError, UnicodeDecodeError):
        return None  # not a layout this reader understands: the harness falls back to estimates


class _Buf:
    """Just enough flatbuffer reading: tables, vectors, strings, scalars."""

    def __init__(self, b):
        self.b = b

    def u8(self, p): return self.b[p]
    def u16(self, p): return struct.unpack_from("<H", self.b, p)[0]
    def u32(self, p): return struct.unpack_from("<I", self.b, p)[0]
    def i32(self, p): return struct.unpack_from("<i", self.b, p)[0]
    def u64(self, p): return struct.unpack_from("<Q", self.b, p)[0]
    def ref(self, p): return p + self.u32(p)

    def field(self, table: int, i: int) -> int | None:
        vt = table - self.i32(table)
        if 4 + 2 * i >= self.u16(vt):
            return None
        off = self.u16(vt + 4 + 2 * i)
        return table + off if off else None

    def vector(self, p: int) -> tuple[int, int]:
        """(length, address of the first element)"""
        v = self.ref(p)
        return self.u32(v), v + 4

    def tables(self, p: int):
        n, start = self.vector(p)
        return [self.ref(start + 4 * j) for j in range(n)]

    def string(self, p: int) -> str:
        n, start = self.vector(p)
        return bytes(self.b[start:start + n]).decode("utf-8")


def _sections(buf: _Buf) -> list[tuple[int, int, int, dict]]:
    """(begin, end, data type, items) for every section in the header."""
    root = buf.ref(32)  # the header flatbuffer starts after magic + version + header end
    sec_meta = buf.ref(buf.field(root, 1))
    out = []
    for obj in buf.tables(buf.field(sec_meta, 0)):
        items = {}
        p = buf.field(obj, 0)
        for kv in buf.tables(p) if p else []:
            key_p, val_p = buf.field(kv, 0), buf.field(kv, 2)
            if key_p and val_p:
                inner = buf.field(buf.ref(val_p), 0)
                try:
                    items[buf.string(key_p)] = buf.string(inner) if inner else None
                except (UnicodeDecodeError, struct.error):
                    pass
        out.append((buf.u64(buf.field(obj, 1)), buf.u64(buf.field(obj, 2)), buf.u8(buf.field(obj, 3)), items))
    return out


def _varint(b, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def _metadata_max_tokens(b, begin: int, end: int) -> int | None:
    """LlmMetadata.max_num_tokens (field 5, varint) from the protobuf section."""
    i = begin
    while i < end:
        key, i = _varint(b, i)
        num, wire = key >> 3, key & 7
        if num == 0:
            break
        if wire == 0:
            val, i = _varint(b, i)
            if num == 5:
                return val or None
        elif wire == 2:
            n, i = _varint(b, i)
            i += n
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:
            break
    return None


def _kv_cache(buf: _Buf, model: int) -> tuple[list[tuple[float, int]], str]:
    """KV-cache tensors of a TFLite model: (bytes per token slot, slots) for each
    distinct cache tensor (k/v per layer, deduplicated across signatures)."""
    root = buf.ref(model)
    per_tensor: dict[tuple[str, int], tuple[float, int]] = {}
    dtypes = set()
    for sub in buf.tables(buf.field(root, 2)):          # Model.subgraphs
        for t in buf.tables(buf.field(sub, 0)):         # SubGraph.tensors
            name_p = buf.field(t, 3)
            m = _KV_NAME.search(buf.string(name_p)) if name_p else None
            if not m:
                continue
            shape_p = buf.field(t, 0)
            n, start = buf.vector(shape_p) if shape_p else (0, 0)
            dims = [buf.i32(start + 4 * d) for d in range(n)]
            if not dims or min(dims) <= 0:
                continue
            type_p = buf.field(t, 1)
            dtype = buf.u8(type_p) if type_p else 0
            dtypes.add(dtype)
            slots = max(dims)  # head dims are at most 512; slots are the long axis
            elems = 1
            for d in dims:
                elems *= d
            entry = (elems // slots * _DTYPE_BYTES.get(dtype, 4), slots)
            key = (m.group(1), int(m.group(2)))
            if entry[1] > per_tensor.get(key, (0, 0))[1]:
                per_tensor[key] = entry
    names = "/".join(sorted(_DTYPE_NAMES.get(d, f"type {d}") for d in dtypes))
    return list(per_tensor.values()), names


def _parse(buf: _Buf) -> ModelInfo | None:
    sections = _sections(buf)
    max_tokens = None
    for begin, end, kind, _ in sections:
        if kind == _SECTION_METADATA:
            max_tokens = _metadata_max_tokens(buf.b, begin, end)
    kv, dtype = [], ""
    tflite = [(b, i) for b, _, k, i in sections if k == _SECTION_TFLITE]
    # The prefill/decode model holds the LLM's cache; other models (drafter, vision) may too.
    tflite.sort(key=lambda s: s[1].get("model_type") != "tf_lite_prefill_decode")
    for begin, _ in tflite:
        kv, dtype = _kv_cache(buf, begin)
        if kv:
            break
    if max_tokens:
        return ModelInfo(max_tokens, "metadata", kv, dtype)
    if kv:
        return ModelInfo(max(s for _, s in kv), "kv-cache", kv, dtype)
    return None
