"""Hybrid RAG search: BM25 plus embeddings."""
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from smallharness.agent import Events, Harness
from smallharness.backends import MockBackend
from smallharness.config import Config
from smallharness.context import RagStore
from smallharness.context.embed import (
    Embedder,
    EmbedError,
    ServerEmbedder,
    make_embedder,
)
from smallharness.pool import ModelPool

# Words with the same meaning share a dimension, the way a real embedding model
# puts synonyms close together.
CONCEPTS = {"undo": ["revert", "rollback", "undo", "back"], "vehicle": ["car", "automobile", "vehicle"],
            "money": ["invoice", "invoices", "payment", "bill", "pay"], "deploy": ["deploy", "release", "ship"]}
WORD_DIM = {w: i for i, ws in enumerate(CONCEPTS.values()) for w in ws}


def concept_vector(text):
    v = [0.0] * len(CONCEPTS) + [0.01]  # small constant so no vector is all zeros
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in WORD_DIM:
            v[WORD_DIM[w]] += 1
    return v


class ConceptEmbedder(Embedder):
    id = "test:concepts"

    def __init__(self, fail=False):
        super().__init__({"min_similarity": 0.3})
        self.fail, self.calls = fail, 0

    def _embed(self, texts, query):
        self.calls += 1
        if self.fail:
            raise EmbedError("server down")
        return [concept_vector(t) for t in texts]


class Hybrid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "ops.md").write_text("To revert a bad release, run the rollback script.\n")
        (self.root / "cars.md").write_text("The automobile fleet is serviced every spring.\n")
        (self.root / "billing.md").write_text("Invoices are sent on the first of the month.\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_by_meaning_where_keywords_miss(self):
        plain = RagStore(None)
        plain.add_path(self.root, self.root)
        self.assertEqual(plain.search("how do I undo a ship"), [])  # no shared words
        hybrid = RagStore(None, ConceptEmbedder())
        hybrid.add_path(self.root, self.root)
        self.assertEqual(hybrid.search("how do I undo a ship")[0].source, "ops.md")
        self.assertEqual(hybrid.search("vehicle maintenance")[0].source, "cars.md")

    def test_keywords_still_count(self):
        store = RagStore(None, ConceptEmbedder())
        store.add_path(self.root, self.root)
        hits = store.search("spring fleet")  # exact words, no concept vector at all
        self.assertEqual(hits[0].source, "cars.md")

    def test_unrelated_query_returns_nothing(self):
        store = RagStore(None, ConceptEmbedder())
        store.add_path(self.root, self.root)
        self.assertEqual(store.search("photosynthesis in plants"), [])

    def test_embedding_failure_falls_back_to_keywords(self):
        emb = ConceptEmbedder(fail=True)
        store = RagStore(None, emb)
        res = store.add_path(self.root, self.root)
        self.assertEqual(res.files, 3)
        self.assertIn("server down", dict(res.skipped)["(embeddings)"])
        self.assertEqual(store.search("rollback")[0].source, "ops.md")
        emb.fail = False  # server back: the next /index fills in the vectors
        self.assertEqual(store.add_path(self.root, self.root).embedded, 3)
        self.assertEqual(store.search("undo")[0].source, "ops.md")

    def test_vectors_persist_and_model_change_reembeds(self):
        idx = self.root / "idx" / "project.json"
        store = RagStore(idx, ConceptEmbedder())
        store.add_path(self.root, self.root)
        again = RagStore(idx, ConceptEmbedder())
        self.assertEqual(again.embedded, 3)  # loaded, not recomputed
        self.assertEqual(again.search("undo")[0].source, "ops.md")
        other = ConceptEmbedder()
        other.id = "test:other-model"
        changed = RagStore(idx, other)
        self.assertEqual(changed.embedded, 0)  # vectors of another model are not comparable
        self.assertEqual(changed.add_path(self.root, self.root).embedded, 3)
        self.assertTrue(idx.with_suffix(".sqlite").is_file())


class EmbeddingsHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        EmbeddingsHandler.requests.append((self.path, body))
        data = [{"index": i, "embedding": concept_vector(t)} for i, t in enumerate(body["input"])]
        out = json.dumps({"data": list(reversed(data))}).encode()  # out of order on purpose
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class ServerEmbeddings(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), EmbeddingsHandler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        EmbeddingsHandler.requests.clear()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_openai_compatible_endpoint(self):
        emb = make_embedder({"backend": "server", "base_url": self.url, "model": "embed-small",
                             "query_prefix": "search_query: ", "batch": 2})
        vecs = emb.embed_documents(["a car", "an invoice", "a release"])
        self.assertEqual(len(vecs), 3)
        self.assertEqual(len(EmbeddingsHandler.requests), 2)  # batched
        path, body = EmbeddingsHandler.requests[0]
        self.assertEqual((path, body["model"]), ("/v1/embeddings", "embed-small"))
        self.assertGreater(vecs[0][WORD_DIM["car"]], 0.9)  # order restored from "index"
        emb.embed_query("car")
        self.assertEqual(EmbeddingsHandler.requests[-1][1]["input"], ["search_query: car"])

    def test_defaults_to_model_server_and_reports_unreachable(self):
        emb = make_embedder({"backend": "server"}, server_url=lambda: self.url)
        self.assertEqual(len(emb.embed_query("car")), len(CONCEPTS) + 1)
        dead = ServerEmbedder({}, lambda: "http://127.0.0.1:9")
        with self.assertRaisesRegex(EmbedError, "unreachable"):
            dead.embed_query("x")
        with self.assertRaisesRegex(EmbedError, "base_url"):
            ServerEmbedder({}, lambda: "").embed_query("x")


class HarnessWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_config_switches_search_mode(self):
        cfg = Config.load(self.ws, overrides={"model": {"backend": "mock"}, "rag": {"embed": {"backend": "none"}}})
        h = Harness(cfg, Events(), backend_factory=lambda: MockBackend())
        h.start()
        self.assertEqual(h.search_mode(), "keywords (BM25)")
        done = h.reconfigure(cfg.with_values({"rag.embed.backend": "server", "rag.embed.base_url": "http://x"}))
        self.assertIs(h.project_rag.embedder, h.embedder)
        self.assertIs(h.session_rag.embedder, h.embedder)
        self.assertTrue(any("hybrid" in d for d in done))

    def test_in_process_embedder_is_counted_in_ram_check(self):
        cfg = Config.load(self.ws, overrides={"model": {"backend": "mock"}, "rag": {"embed": {"backend": "none"}}})
        pool = ModelPool(cfg, MockBackend)
        before = pool.report()
        cfg.data["rag"]["embed"]["backend"] = "fastembed"
        after = pool.report()
        self.assertAlmostEqual(before.usable_mb - after.usable_mb, 300, delta=50)


if __name__ == "__main__":
    unittest.main()
