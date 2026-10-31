"""RAG store: chunked documents searched by keywords (BM25) and, with an embedding
model, by meaning, the two rankings merged by Reciprocal Rank Fusion. Pure stdlib.

Each index is an SQLite file (WAL mode), so a write changes only the rows it touches,
and several processes (a chat, `smallharness notes`, the web interface) can use the
same index: each one notices commits made by the others and reloads before it reads.
Searches run over an in-memory copy that is replaced, never changed in place, so
threads read a consistent snapshot.

`search(stores, query)` ranks several indexes together, as one collection: keyword
statistics are counted over all of them, so a score from one index means the same as
a score from another."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from array import array
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .embed import Embedder, EmbedError, pack_matrix, similarities_matrix, unpack
from .extract import ExtractError, extract, kind_of, size_limit_mb

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were "
    "will with what how why when where which who do does did can i you we they not".split())

SKIP_DIRS = {".git", ".smallharness", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

INDEX_VERSION = 3  # bump when terms() changes; older indexes are re-tokenized on load
K1, B = 1.5, 0.75   # BM25
RRF_K = 60          # Reciprocal Rank Fusion constant
_VOWEL = re.compile(r"[aeiouy]")


def stem(w: str) -> str:
    """A light suffix stripper that brings every form of a word to the same stem:
    "deploy", "deploys", "deployed" and "deploying" all give "deploy"; "complex" and
    "complexes", "run", "runs" and "running", "use", "uses" and "used" match too."""
    if len(w) > 4 and w.endswith(("ies", "ied")):
        w = w[:-3] + "y"
    elif len(w) > 5 and w.endswith("sis"):           # analysis, like analyses
        w = w[:-2]
    elif w.endswith(("sses", "xes", "ches", "shes", "zzes")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for _ in range(2):   # twice, so "embedded" and "needed" reach the stem of "embed" and "need"
        for suffix in ("ing", "ed"):
            base = w[:-len(suffix)]
            if w.endswith(suffix) and len(base) >= 2 and _VOWEL.search(base):
                w = _undouble(base)
                break
        else:
            break
    while len(w) >= 3 and w.endswith("e"):            # use, uses, used, using: "us"
        w = w[:-1]
    return _undouble(w)


def _undouble(w: str) -> str:
    """running, run; but fall and pass keep theirs."""
    return w[:-1] if len(w) >= 3 and w[-1] == w[-2] and w[-1] not in "aeiouylsz" else w


def terms(text: str) -> list[str]:
    """Lowercase stemmed words, stopwords dropped. An identifier written in camel case
    gives its parts and the whole word, so "BamA" finds "BamA" before "BAM", and
    "getUser" finds both "getUser" and "user"."""
    out = []
    for word in _TOKEN.findall(text):
        parts = _CAMEL.sub(" ", word).split()
        for w in [p.lower() for p in parts] + ([word.lower()] if len(parts) > 1 else []):
            if w not in STOPWORDS and len(w) > 1:
                out.append(stem(w))
    return out


@dataclass
class Chunk:
    source: str
    start_line: int
    text: str
    score: float = 0.0

    def cite(self) -> str:
        return f"{self.source}:{self.start_line}"


# ---------------------------------------------------------------- chunking
_MARKDOWN = (".md", ".markdown", ".rst", ".txt")
_HEADING = re.compile(r"#{1,6}\s|\S.*\n[=-]{3,}\s*$")
_CODE_START = re.compile(r"(?:async\s+def|def|class|function|func|fn|impl|interface|struct|enum|"
                         r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const)|"
                         r"(?:public|private|protected|static)\s)\b|@\w")


def _starts_section(lines: list[str], i: int, kind: str) -> bool:
    """Line i opens a top-level section: a Markdown heading, or a definition (with its
    decorators) at the start of a line in code."""
    line = lines[i]
    if kind == "md":
        return bool(_HEADING.match(line)) and not line.startswith("#!")
    if kind == "code" and _CODE_START.match(line):
        return not (i and lines[i - 1].startswith("@"))   # a decorator already opened it
    return False


def chunk_text(text: str, max_chars: int = 1200, overlap_lines: int = 3, source: str = "") -> list[tuple[int, str]]:
    """Split into ~max_chars chunks of whole lines. A new top-level section (a heading in
    Markdown, a function or class in code, by the source's suffix) starts a new chunk
    once the current one has some substance; otherwise blank lines are preferred. A
    chunk cut mid-section repeats its last lines at the start of the next."""
    suffix = Path(source.split(":", 1)[-1]).suffix.lower()
    kind = "md" if suffix in _MARKDOWN else "code" if suffix and suffix not in _MARKDOWN else ""
    lines = text.splitlines()
    chunks, start, buf, size = [], 0, [], 0

    def flush(next_line: int, keep: list[str]) -> None:
        """Close the chunk; the next one starts at `next_line` (0-based), after `keep`."""
        nonlocal start, buf, size
        chunks.append((start + 1, "\n".join(buf)))
        start, buf, size = next_line - len(keep), list(keep), sum(len(x) + 1 for x in keep)

    for i, line in enumerate(lines):
        if buf and size >= max_chars * 0.25 and _starts_section(lines, i, kind):
            flush(i, [])
        buf.append(line)
        size += len(line) + 1
        boundary = not line.strip() and size > max_chars * 0.6
        if size >= max_chars or boundary:
            flush(i + 1, buf[-overlap_lines:] if overlap_lines and not boundary else [])
    if any(x.strip() for x in buf):
        chunks.append((start + 1, "\n".join(buf)))
    return chunks


@dataclass
class IndexResult:
    files: int = 0                    # files (re)indexed
    chunks: int = 0
    unchanged: int = 0                # already indexed and not modified since
    removed: int = 0                  # indexed earlier, deleted from disk since
    embedded: int = 0                 # chunks given vectors that had none (new embedder, retry)
    ignored: int = 0                  # file types /index does not accept
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file, reason)

    def describe(self) -> str:
        parts = [f"indexed {self.files} file(s) into {self.chunks} chunk(s)"]
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if self.embedded:
            parts.append(f"{self.embedded} chunk(s) embedded")
        if self.removed:
            parts.append(f"{self.removed} deleted file(s) removed from the index")
        if self.ignored:
            parts.append(f"{self.ignored} of other types ignored")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        return ", ".join(parts)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY, mtime REAL);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, source TEXT NOT NULL, start_line INTEGER,
                                   text TEXT, tf TEXT, len INTEGER, vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks (source);
"""


def _vector(blob) -> array | None:
    if blob is None:
        return None
    v = array("f")
    v.frombytes(blob)
    return v


class Snapshot:
    """The index as of one moment: never changed, replaced on every write. What makes
    search fast is built once per snapshot, on the first search after a change: a
    posting list per term (BM25 touches only chunks containing the query's terms)
    and the vectors packed into one matrix (one multiplication per query)."""

    def __init__(self, chunks: list[dict], df: Counter, sources: dict[str, float]):
        self.chunks, self.df, self.sources = chunks, df, sources
        self.total_len = sum(c["len"] for c in chunks)
        self._postings: dict[str, list[tuple[int, int]]] | None = None
        self._vectors = None

    @property
    def postings(self) -> dict[str, list[tuple[int, int]]]:
        if self._postings is None:
            postings: dict[str, list[tuple[int, int]]] = {}
            for i, c in enumerate(self.chunks):
                for term, f in c["tf"].items():
                    postings.setdefault(term, []).append((i, f))
            self._postings = postings
        return self._postings

    def vectors(self) -> tuple[list[int], object]:
        """(chunk positions, matrix) for the chunks that have vectors."""
        if self._vectors is None:
            rows = [i for i, c in enumerate(self.chunks) if "vec" in c]
            self._vectors = (rows, pack_matrix([self.chunks[i]["vec"] for i in rows]))
        return self._vectors


class RagStore:
    def __init__(self, path: Path | None, embedder: Embedder | None = None):
        """`path` names the index, kept as an SQLite file (`.sqlite`); an index saved by an
        earlier version under the same name (`.json`) is imported once. path=None keeps it in memory.
        With an embedder, search is hybrid: BM25 and vector similarity ranked together."""
        self.path = path.with_suffix(".sqlite") if path else None
        self._lock = threading.RLock()
        self._batch = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path) if self.path else ":memory:", timeout=30,
                                   check_same_thread=False, isolation_level=None)
        if self.path:
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self.embedder: Embedder | None = None
        self.embed_error = ""                 # last embedding failure, for the UI
        legacy = path.with_suffix(".json") if path else None
        if legacy and legacy.is_file() and not self._meta("version"):
            self._import_json(legacy)
        if self._meta("version") != str(INDEX_VERSION):
            self._retokenize()
        self._data_version = None
        self._fresh()
        self.set_embedder(embedder)

    # -------------------------------------------------------------- storage
    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    @contextmanager
    def _write(self):
        """One transaction; nested writes (a batch) commit once, at the end."""
        with self._lock:
            if self._batch == 0:
                self._db.execute("BEGIN IMMEDIATE")
                self._fresh()
            self._batch += 1
            try:
                yield
            except BaseException:
                self._batch -= 1
                if self._batch == 0:
                    self._db.execute("ROLLBACK")
                    self._load()
                raise
            self._batch -= 1
            if self._batch == 0:
                self._db.execute("COMMIT")
                self._data_version = self._db.execute("PRAGMA data_version").fetchone()[0]

    def _fresh(self) -> None:
        """Reload if another process (or connection) committed since we last read."""
        with self._lock:
            v = self._db.execute("PRAGMA data_version").fetchone()[0]
            if v != self._data_version:
                self._load()
                self._data_version = v

    def _load(self) -> None:
        chunks = []
        for cid, source, start, text, tf, length, vec in self._db.execute(
                "SELECT id, source, start_line, text, tf, len, vec FROM chunks ORDER BY id"):
            c = {"id": cid, "source": source, "start_line": start, "text": text, "tf": json.loads(tf), "len": length}
            if vec is not None:
                c["vec"] = _vector(vec)
            chunks.append(c)
        df: Counter = Counter()
        for c in chunks:
            df.update(c["tf"].keys())
        self._snap = Snapshot(chunks, df, dict(self._db.execute("SELECT source, mtime FROM sources")))
        self.embed_id = self._meta("embed_id")

    def _import_json(self, path: Path) -> None:
        """An index saved by an earlier version, as one JSON file; renamed once imported."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        self._db.execute("BEGIN IMMEDIATE")
        for c in data.get("chunks", []):
            vec = unpack(c["vec"]).tobytes() if c.get("vec") else None
            self._db.execute("INSERT INTO chunks (source, start_line, text, tf, len, vec) VALUES (?, ?, ?, ?, ?, ?)",
                             (c["source"], c["start_line"], c["text"], json.dumps(c.get("tf", {})), c.get("len", 0), vec))
        self._db.executemany("INSERT OR REPLACE INTO sources VALUES (?, ?)", list(data.get("sources", {}).items()))
        if data.get("embed_id"):
            self._set_meta("embed_id", data["embed_id"])
        self._set_meta("version", str(data.get("version", 1)))
        self._db.execute("COMMIT")
        path.rename(path.with_suffix(".json.imported"))

    def _retokenize(self) -> None:
        """The stored text is kept, so a tokenizer change only recomputes the terms."""
        self._db.execute("BEGIN IMMEDIATE")
        for cid, source, text in list(self._db.execute("SELECT id, source, text FROM chunks")):
            t = terms(text + " " + source)
            self._db.execute("UPDATE chunks SET tf = ?, len = ? WHERE id = ?", (json.dumps(Counter(t)), len(t), cid))
        self._set_meta("version", str(INDEX_VERSION))
        self._db.execute("COMMIT")

    def save(self) -> None:
        """Every change is committed as it is made; kept for callers of the old API."""

    def close(self) -> None:
        self._db.close()

    def __del__(self):   # stores made for one task are simply dropped
        try:
            self._db.close()
        except Exception:  # noqa: BLE001 - nothing to report while being collected
            pass

    # ----------------------------------------------------------- reading
    def snapshot(self) -> Snapshot:
        """The index as of now; replaced on every write, never changed."""
        self._fresh()
        return self._snap

    @property
    def chunks(self) -> list[dict]:
        return self.snapshot().chunks

    @property
    def sources(self) -> dict[str, float]:
        """source -> its file's mtime when indexed."""
        return self.snapshot().sources

    def __len__(self) -> int:
        return len(self.chunks)

    @property
    def embedded(self) -> int:
        return sum(1 for c in self.chunks if "vec" in c)

    def to_dict(self) -> dict:
        return {"chunks": len(self.chunks), "sources": len(self.sources)}

    # -------------------------------------------------------- embeddings
    def set_embedder(self, embedder: Embedder | None) -> None:
        """Vectors from another model are not comparable; they are dropped and made again
        by embed_missing() (run at the end of every add_path)."""
        self.embedder = embedder
        if embedder and self.embed_id != embedder.id:
            with self._write():
                self._db.execute("UPDATE chunks SET vec = NULL")
                self._set_meta("embed_id", embedder.id)
            self._load()

    def _embed_chunks(self, chunks: list[dict]) -> int:
        """Vectors for `chunks` (dicts not yet stored, or stored ones); '' error on success."""
        if not self.embedder or not chunks:
            return 0
        try:
            vecs = self.embedder.embed_documents([f"{c['source']}\n{c['text']}" for c in chunks])
        except EmbedError as e:
            self.embed_error = str(e)
            return 0
        for c, v in zip(chunks, vecs):
            c["vec"] = v
        self.embed_error = ""
        return len(chunks)

    def embed_missing(self) -> int:
        """Embed chunks that have no vector yet (new embedder, or a server that was down)."""
        missing = [dict(c) for c in self.chunks if "vec" not in c]
        n = self._embed_chunks(missing)
        if n:
            with self._write():
                self._db.executemany("UPDATE chunks SET vec = ? WHERE id = ?",
                                     [(c["vec"].tobytes(), c["id"]) for c in missing if "vec" in c])
            self._load()
        return n

    # ------------------------------------------------------------- writing
    @contextmanager
    def batch(self):
        """Group many changes into one transaction (indexing a folder)."""
        with self._write():
            yield

    def add_text(self, source: str, text: str, mtime: float = 0.0, save: bool = True) -> int:
        new = []
        for start, body in chunk_text(text, source=source):
            t = terms(body + " " + source)
            if t:
                new.append({"source": source, "start_line": start, "text": body, "tf": dict(Counter(t)), "len": len(t)})
        self._embed_chunks(new)       # before the transaction: it may call a server
        with self._write():
            self._db.execute("DELETE FROM chunks WHERE source = ?", (source,))
            for c in new:
                vec = c["vec"].tobytes() if "vec" in c else None
                c["id"] = self._db.execute(
                    "INSERT INTO chunks (source, start_line, text, tf, len, vec) VALUES (?, ?, ?, ?, ?, ?)",
                    (source, c["start_line"], c["text"], json.dumps(c["tf"]), c["len"], vec)).lastrowid
            self._db.execute("INSERT OR REPLACE INTO sources VALUES (?, ?)", (source, mtime))
            self._replace(source, new, mtime)
        return len(new)

    def remove(self, source: str, save: bool = True) -> None:
        if source not in self.sources:
            return
        with self._write():
            self._db.execute("DELETE FROM chunks WHERE source = ?", (source,))
            self._db.execute("DELETE FROM sources WHERE source = ?", (source,))
            self._replace(source, [], None)

    def _replace(self, source: str, new: list[dict], mtime: float | None) -> None:
        """The in-memory copy after a write: new lists, so readers keep a consistent one."""
        snap = self._snap
        chunks, df, sources = snap.chunks, Counter(snap.df), snap.sources
        keep = []
        for c in chunks:
            if c["source"] == source:
                df.subtract(c["tf"].keys())
            else:
                keep.append(c)
        for c in new:
            df.update(c["tf"].keys())
        sources = dict(sources)
        if mtime is None:
            sources.pop(source, None)
        else:
            sources[source] = mtime
        self._snap = Snapshot(keep + new, +df, sources)

    def add_path(self, path: Path, root: Path, rag_cfg: dict | None = None,
                 progress: Callable[[str], None] | None = None) -> IndexResult:
        """Index a file or a whole folder tree. Only the types in [rag] are accepted;
        PDFs and images go through OCR. Unchanged files are skipped."""
        if rag_cfg is None:
            from ..config import DEFAULTS
            rag_cfg = DEFAULTS["rag"]
        res = IndexResult()
        if path.is_file():
            files = [path]
        else:
            files = sorted(p for p in path.rglob("*") if p.is_file()
                           and not any(part in SKIP_DIRS for part in p.relative_to(path).parts))
        for f in files:
            try:
                rel = str(f.resolve().relative_to(root))
            except ValueError:
                rel = str(f)
            kind = kind_of(f, rag_cfg)
            if kind is None:
                if path.is_file():
                    res.skipped.append((rel, f"file type {f.suffix or '(none)'} is not accepted"))
                else:
                    res.ignored += 1
                continue
            st = f.stat()
            if self.sources.get(rel) == st.st_mtime:
                res.unchanged += 1
                continue
            limit = size_limit_mb(kind, rag_cfg)
            if st.st_size > limit * 1_000_000:
                res.skipped.append((rel, f"larger than {limit} MB"))
                continue
            if progress:
                progress(f"{'reading' if kind == 'text' else 'OCR'} {rel}")
            try:
                text = extract(f, kind, rag_cfg)
            except (ExtractError, OSError) as e:
                res.skipped.append((rel, str(e)))
                continue
            if not text.strip():
                res.skipped.append((rel, "no text found"))
                continue
            res.chunks += self.add_text(rel, text, st.st_mtime)
            res.files += 1
        if path.is_dir():
            res.removed = self._prune(path, root)
        if self.embedder:
            if progress:
                progress("embedding")
            res.embedded = self.embed_missing()  # earlier failures, or a new embedding model
            if self.embed_error:
                res.skipped.append(("(embeddings)", self.embed_error + "; search uses keywords only"))
        return res

    def _prune(self, folder: Path, root: Path) -> int:
        """Drop indexed files under `folder` that no longer exist."""
        gone = []
        for src in self.sources:
            if ":" in src:  # not a file (e.g. archived session turns)
                continue
            f = Path(src) if Path(src).is_absolute() else root / src
            if f.resolve().is_relative_to(folder.resolve()) and not f.exists():
                gone.append(src)
        with self.batch():
            for src in gone:
                self.remove(src)
        return len(gone)

    # ---------------------------------------------------------------- search
    def search(self, query: str, k: int = 3, min_relative: float = 0.3,
               where: Callable[[str], bool] | None = None) -> list[Chunk]:
        return search([self], query, k, min_relative, where)


def search(stores: Iterable[RagStore], query: str, k: int = 3, min_relative: float = 0.3,
           where: Callable[[str], bool] | None = None) -> list[Chunk]:
    """The k best chunks for `query` across `stores`, ranked as one collection; with
    `where(source)`, only chunks from sources it accepts.

    Keywords (BM25), with term statistics over all the stores: chunks scoring under
    `min_relative` times the best score are dropped (BM25 scores depend on the
    collection's size, so the cutoff is relative). With an embedder, chunks are also
    ranked by meaning (cosine similarity of at least [rag.embed].min_similarity and
    within similarity_margin of the best chunk anywhere, in scope or not), and the
    two rankings are merged by Reciprocal Rank Fusion: a chunk ranked high by either
    comes first, and by both, higher still. Scores run from 0 to 1 either way."""
    snaps = [(s, s.snapshot()) for s in stores if s is not None]   # consistent copies
    lexical = _bm25(snaps, query, min_relative, where)
    vector = _vector_ranking(snaps, query, where)
    depth = max(k * 4, 20)
    if not vector:
        top = lexical[0][0] if lexical else 1.0
        return [Chunk(c["source"], c["start_line"], c["text"], round(s / top, 2)) for s, c in lexical[:k]]
    fused: dict[int, list] = {}
    for ranking in (lexical[:depth], vector[:depth]):
        for rank, (_, c) in enumerate(ranking):
            entry = fused.setdefault(id(c), [0.0, c])
            entry[0] += 1 / (RRF_K + rank + 1)
    best = sorted(fused.values(), key=lambda e: -e[0])[:k]
    top = 2 / (RRF_K + 1)   # a chunk ranked first by both methods scores 1.0
    return [Chunk(c["source"], c["start_line"], c["text"], round(s / top, 2)) for s, c in best]


def _bm25(snaps, query: str, min_relative: float, where=None) -> list[tuple[float, dict]]:
    q = set(terms(query))
    n = sum(len(sn.chunks) for _, sn in snaps)
    if not q or not n:
        return []
    avg = sum(sn.total_len for _, sn in snaps) / n
    df = {t: sum(sn.df.get(t, 0) for _, sn in snaps) for t in q}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
    scored = []
    for _, sn in snaps:
        acc: dict[int, float] = {}
        for t in q:
            for i, f in sn.postings.get(t, ()):
                c = sn.chunks[i]
                acc[i] = acc.get(i, 0.0) + idf[t] * f * (K1 + 1) / (f + K1 * (1 - B + B * c["len"] / avg))
        for i, s in acc.items():
            c = sn.chunks[i]
            if where is None or where(c["source"]):
                scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    if scored:
        floor = scored[0][0] * min_relative
        scored = [x for x in scored if x[0] >= floor]
    return scored


def _vector_ranking(snaps, query: str, where=None) -> list[tuple[float, dict]]:
    """Cosine similarity over the chunks embedded by the same model as the query."""
    embedder = next((s.embedder for s, _ in snaps if s.embedder), None)
    if embedder is None or not query.strip():
        return []
    usable = [(s, sn) for s, sn in snaps if s.embed_id == embedder.id and sn.vectors()[0]]
    if not usable:
        return []
    try:
        qv = embedder.embed_query(query)
    except EmbedError as e:
        for s, _ in snaps:
            s.embed_error = str(e)
        return []  # keyword search still works
    ranked, best = [], 0.0
    for _, sn in usable:
        rows, matrix = sn.vectors()
        for i, sim in zip(rows, similarities_matrix(qv, matrix)):
            best = max(best, sim)
            c = sn.chunks[i]
            if where is None or where(c["source"]):
                ranked.append((sim, c))
    # Far below the best match anywhere (in scope or not) is not about the question.
    floor = max(embedder.min_similarity, best - float(embedder.cfg.get("similarity_margin", 0.15)))
    ranked = [x for x in ranked if x[0] >= floor]
    ranked.sort(key=lambda x: -x[0])
    return ranked


__all__ = ["RagStore", "Chunk", "IndexResult", "chunk_text", "search", "terms"]
