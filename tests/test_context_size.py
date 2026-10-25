"""Context size from the model file, exact KV-cache RAM, and token-count calibration."""
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smallharness import resources
from smallharness.config import Config
from smallharness.modelinfo import ModelInfo, read_model_info
from smallharness.resources import RamInfo, choose_context
from smallharness.tokens import TokenCounter, estimate_tokens

REAL_MODEL = Path("/workspace/.litert-lm/cache/huggingface/litert-community/"
                  "gemma-4-E2B-it-litert-lm/gemma-4-E2B-it.litertlm")


# ------------------------------------------------- a tiny flatbuffer writer
class T(dict):
    """A table: {field index: value}. Values: ("u8"|"u64", n), Str, Vec, IntVec or T."""


class Str(str):
    pass


class Vec(list):
    """A vector of tables."""


class IntVec(list):
    """A vector of int32."""


def flatbuffer(root: T) -> bytes:
    """Serialize with every child placed after its parent, so all offsets point forward."""
    buf = bytearray(4)
    pending = [(0, root)]  # (position of the uoffset to patch, object)

    def put(obj) -> int:
        while len(buf) % 4:
            buf.append(0)
        if isinstance(obj, Str):
            pos = len(buf)
            data = obj.encode()
            buf.extend(struct.pack("<I", len(data)) + data + b"\0")
            return pos
        if isinstance(obj, IntVec):
            pos = len(buf)
            buf.extend(struct.pack("<I", len(obj)) + b"".join(struct.pack("<i", x) for x in obj))
            return pos
        if isinstance(obj, Vec):
            pos = len(buf)
            buf.extend(struct.pack("<I", len(obj)))
            for child in obj:
                pending.append((len(buf), child))
                buf.extend(b"\0\0\0\0")
            return pos
        n = max(obj) + 1 if obj else 0  # a table: vtable, then 8-byte slots
        vt = len(buf)
        buf.extend(struct.pack("<HH", 4 + 2 * n, 4 + 8 * n))
        buf.extend(b"".join(struct.pack("<H", 4 + 8 * i if i in obj else 0) for i in range(n)))
        while len(buf) % 4:
            buf.append(0)
        pos = len(buf)
        buf.extend(struct.pack("<i", pos - vt) + bytes(8 * n))
        for i, v in obj.items():
            slot = pos + 4 + 8 * i
            if isinstance(v, tuple):
                kind, val = v
                struct.pack_into("<B" if kind == "u8" else "<Q", buf, slot, val)
            else:
                pending.append((slot, v))
        return pos

    while pending:
        at, obj = pending.pop(0)
        struct.pack_into("<I", buf, at, put(obj) - at)
    return bytes(buf)


def varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def kv_tensor(name, shape, dtype):
    return T({0: IntVec(shape), 1: ("u8", dtype), 3: Str(name)})


def litertlm(max_tokens=None, slots=4096, layers=2, local_slots=None) -> bytes:
    """A .litertlm with a metadata section and a prefill/decode TFLite model whose KV
    cache has `slots` token slots (int8, 1 head x 256 dims per layer)."""
    proto = b"\x0a\x03\x0a\x01\x02"  # field 1: start token
    if max_tokens:
        proto += b"\x28" + varint(max_tokens)  # field 5, varint
    proto += b"\x3a\x04abcd"  # field 7: a chat template string, skipped by the reader
    tensors = []
    for layer in range(layers):
        n = local_slots if (local_slots and layer == 0) else slots
        tensors += [kv_tensor(f"decode_kv_cache_k_{layer}:0", [1, 1, n, 256], 9),
                    kv_tensor(f"decode_kv_cache_v_{layer}:0", [1, 1, 256, n], 9),
                    kv_tensor(f"prefill_kv_cache_k_{layer}:0", [1, 1, n, 256], 9)]  # same cache, other signature
    tensors.append(T({0: IntVec([1, 128]), 1: ("u8", 0), 3: Str("tokens")}))
    tflite = flatbuffer(T({2: Vec([T({0: Vec(tensors)})])}))
    tflite = tflite[:4] + b"TFL3" + tflite[8:] if len(tflite) > 8 else tflite
    proto_at, tflite_at = 4096, 8192

    def section(begin, end, kind, items=()):
        kvs = Vec([T({0: Str(k), 1: ("u8", 9), 2: T({0: Str(v)})}) for k, v in items])
        return T({0: kvs, 1: ("u64", begin), 2: ("u64", end), 3: ("u8", kind)})

    header = flatbuffer(T({0: T({}), 1: T({0: Vec([
        section(proto_at, proto_at + len(proto), 5),
        section(tflite_at, tflite_at + len(tflite), 3, [("model_type", "tf_lite_prefill_decode")]),
    ])})}))
    out = bytearray(b"LITERTLM" + struct.pack("<IIIIQ", 1, 5, 0, 0, 32 + len(header)) + header)
    out += bytes(proto_at - len(out)) + proto
    out += bytes(tflite_at - len(out)) + tflite
    return bytes(out)


# ---------------------------------------------------------------- tests
class ReadModelFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data: bytes, name="m.litertlm") -> str:
        p = self.dir / name
        p.write_bytes(data)
        return str(p)

    def test_context_from_kv_cache(self):
        info = read_model_info(self.write(litertlm(slots=4096, layers=2)))
        self.assertEqual((info.max_context, info.source, info.kv_dtype), (4096, "kv-cache", "int8"))
        self.assertEqual(len(info.kv_layers), 4)  # k and v of 2 layers; prefill copies deduplicated
        self.assertEqual(info.kv_bytes_per_token, 4 * 256)
        self.assertEqual(info.kv_mb(2048), 3)  # 2 MiB, rounded up

    def test_metadata_max_tokens_wins(self):
        info = read_model_info(self.write(litertlm(max_tokens=1280, slots=4096)))
        self.assertEqual((info.max_context, info.source), (1280, "metadata"))

    def test_sliding_window_layers_are_capped(self):
        info = read_model_info(self.write(litertlm(slots=8192, layers=2, local_slots=512)))
        self.assertEqual(info.max_context, 8192)
        full = 2 * 256 * 8192 + 2 * 256 * 512  # local layer only holds 512 tokens
        self.assertEqual(info.kv_mb(8192), int(full / 2**20) + 1)

    def test_other_files_are_ignored(self):
        self.assertIsNone(read_model_info(self.write(b"PK\x03\x04 a .task zip", "m.task")))
        self.assertIsNone(read_model_info(self.write(b"LITERTLM" + bytes(40), "broken.litertlm")))
        self.assertIsNone(read_model_info(str(self.dir / "missing.litertlm")))

    @unittest.skipUnless(REAL_MODEL.is_file(), "the user's gemma-4-E2B-it model is not here")
    def test_real_gemma4_model(self):
        info = read_model_info(str(REAL_MODEL))
        self.assertEqual(info.max_context, 32003)
        self.assertAlmostEqual(info.kv_bytes_per_token / 1024, 9.0, places=1)


class ChooseContext(unittest.TestCase):
    INFO = ModelInfo(32003, "kv-cache", [(4608.0, 32003), (4608.0, 32003)], "int8")  # 9 KB/token
    RES = {"manage_ram": True, "reserve_ram_mb": 1024, "overhead_factor": 1.25, "kv_cache_mb": 0, "model_mb": 0}

    def choose(self, setting, free_mb=16000, cap=8192, res=None):
        with mock.patch.object(resources, "read_model_info", return_value=self.INFO), \
                mock.patch.object(resources, "ram_info", return_value=RamInfo(32000, free_mb, "test")), \
                mock.patch.object(resources, "model_file_mb", return_value=2468):
            return choose_context({"backend": "litert", "context_tokens": setting, "auto_context_max": cap},
                                  res or self.RES, "/models/gemma.litertlm")

    def test_auto_uses_model_max_up_to_cap(self):
        self.assertEqual(self.choose("auto").tokens, 8192)
        self.assertEqual(self.choose("auto", cap=0).tokens, 32003)
        self.assertEqual(self.choose(0, cap=65536).tokens, 32003)

    def test_auto_shrinks_to_fit_ram(self):
        # 4300 MB free - 1024 reserve - 3085 weights leaves 191 MB: 16K (141 MB) fits, 32K (282 MB) not
        c = self.choose("auto", free_mb=4300, cap=0)
        self.assertEqual(c.tokens, 16001)
        self.assertIn("fit free RAM", c.note)
        unmanaged = self.choose("auto", free_mb=4300, cap=0, res={**self.RES, "manage_ram": False})
        self.assertEqual(unmanaged.tokens, 32003)

    def test_explicit_value_is_kept_but_capped_at_model_max(self):
        self.assertEqual(self.choose(2048).tokens, 2048)
        c = self.choose(100000)
        self.assertEqual(c.tokens, 32003)
        self.assertIn("above the model's maximum", c.note)

    def test_unknown_model_falls_back(self):
        c = choose_context({"backend": "litert", "context_tokens": "auto"}, self.RES, "gemma-4-E2B-it")
        self.assertEqual(c.tokens, 4096)
        self.assertIn("unknown", c.note)

    def test_exact_kv_replaces_heuristic_in_ram_check(self):
        with mock.patch.object(resources, "read_model_info", return_value=self.INFO):
            exact = resources.kv_cache_mb("/m.litertlm", 2468, 8192, self.RES)
        self.assertEqual(exact, 73)
        self.assertEqual(resources.kv_cache_mb("", 2468, 8192, self.RES),
                         resources.estimate_kv_cache_mb(2468, 8192))  # no file: heuristic

    def test_config_resolves_once_per_model(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config.load(Path(d), overrides={"model": {"path": "/models/gemma.litertlm"}})
            with mock.patch.object(resources, "choose_context",
                                   return_value=resources.ContextChoice(8192)) as choose:
                self.assertEqual((cfg.context_tokens, cfg.context_tokens), (8192, 8192))
                self.assertEqual(choose.call_count, 1)
                cfg.data["model"]["context_tokens"] = 2048
                cfg.context_tokens
                self.assertEqual(choose.call_count, 2)


class Calibration(unittest.TestCase):
    TEXT = "def handler(event):\n    return {'status': 200, 'body': event['payload']}\n" * 40

    def test_learns_from_server_counts(self):
        c = TokenCounter()
        est = estimate_tokens(self.TEXT)
        self.assertEqual(c(self.TEXT), est)
        c.observe(self.TEXT, int(est * 1.3))  # the real tokenizer splits code finer
        self.assertGreaterEqual(c(self.TEXT), int(est * 1.3))  # never under the real count
        self.assertLess(c(self.TEXT), int(est * 1.3 * 1.1))
        self.assertIn("calibrated from 1", c.describe())

    def test_ignores_tiny_prompts_and_resets(self):
        c = TokenCounter()
        c.observe("hi", 50)
        self.assertEqual(c.samples, 0)
        c.observe(self.TEXT, 10_000_000)
        self.assertEqual(c.scale, 1.5)  # clamped
        c.reset()
        self.assertEqual((c.scale, c.samples), (1.0, 0))

    def test_server_usage_reaches_the_counter(self):
        from smallharness.agent import Events, Harness
        from smallharness.backends import MockBackend

        class Counting(MockBackend):
            def generate(self, prompt, max_tokens, stop=None):
                self.last_prompt_tokens = int(estimate_tokens(prompt) * 1.2)
                return "ok"

        with tempfile.TemporaryDirectory() as d:
            cfg = Config.load(Path(d), overrides={"model": {"backend": "mock"}})
            h = Harness(cfg, Events(), backend_factory=Counting)
            h.start()
            h.generate(self.TEXT)
            self.assertEqual(h.counter.samples, 1)
            self.assertAlmostEqual(h.counter.scale, 1.2, places=2)


if __name__ == "__main__":
    unittest.main()
