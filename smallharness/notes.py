"""Notes: short Markdown files in `.smallharness/notes/`, indexed into the project
RAG when saved: part of the research knowledge base.

Tags are optional: `#word` anywhere in the text (a `#` followed by a letter, so
Markdown headings like `# Title` are not tags). They are kept lowercase and can
filter listings and searches.

Each note is `<id>.md` with nothing else in it, so notes can be edited with any
editor; changed or deleted files are picked up the next time the notes are opened.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .context.rag import Chunk, RagStore
from .errors import Invalid, NotFound

TAG = re.compile(r"(?<![\w#&/])#([A-Za-z][\w\-/]*)")
SOURCE = "note:"
_TAGS_LINE = re.compile(r"\n*tags: (?:#\S+ ?)+\s*$")


def tags_of(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for t in TAG.findall(text):
        seen.setdefault(t.lower().rstrip("-/"), None)
    return list(seen)


def parse_id(value) -> int:
    """`12`, `"12"`, `"[12]"` or `"note:12"` -> 12."""
    text = str(value).strip().strip("[]")
    text = text[len(SOURCE):] if text.startswith(SOURCE) else text
    if not text.isdigit():
        raise Invalid(f"{value!r} is not a note number")
    return int(text)


@dataclass
class Note:
    id: int
    text: str
    mtime: float

    @property
    def title(self) -> str:
        first = next((ln.strip() for ln in self.text.splitlines() if ln.strip()), "(empty)")
        first = first.lstrip("#").strip() or "(empty)"
        return first if len(first) <= 70 else first[:69] + "…"

    @property
    def tags(self) -> list[str]:
        return tags_of(self.text)

    @property
    def source(self) -> str:
        return f"{SOURCE}{self.id}"

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "tags": self.tags, "text": self.text, "mtime": self.mtime}


class NoteStore:
    """Notes as Markdown files, indexed into the project RAG: part of the research
    knowledge base."""

    def __init__(self, directory: Path, rag: RagStore):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rag = rag
        self.sync()

    @classmethod
    def for_workspace(cls, state, rag: RagStore) -> "NoteStore":
        """The workspace's notes, with CAG files from earlier versions imported once."""
        store = cls(state.notes, rag)
        legacy = state.root / "cag"
        if legacy.is_dir() and any(legacy.glob("*.md")):
            store.import_cag(legacy)
        return store

    def _path(self, note_id: int) -> Path:
        return self.dir / f"{note_id}.md"

    def _files(self) -> dict[int, Path]:
        return {int(p.stem): p for p in self.dir.glob("*.md") if p.stem.isdigit()}

    def sync(self) -> int:
        """Bring the index in line with the files: (re)index notes changed outside the
        harness, drop deleted ones. Returns the number of changes."""
        files = self._files()
        changed = 0
        for nid, p in files.items():
            mtime = p.stat().st_mtime
            if self.rag.sources.get(f"{SOURCE}{nid}") != mtime:
                self._index(Note(nid, p.read_text(encoding="utf-8", errors="replace"), mtime), save=False)
                changed += 1
        for src in [s for s in self.rag.sources if s.startswith(SOURCE)]:
            if src[len(SOURCE):].isdigit() and int(src[len(SOURCE):]) not in files:
                self.rag.remove(src, save=False)
                changed += 1
        if changed:
            self.rag.save()
        return changed

    def _index(self, note: Note, save: bool = True) -> None:
        # Tags are repeated at the end so a tag search also matches a long note's later chunks.
        tags = " ".join(f"#{t}" for t in note.tags)
        self.rag.add_text(note.source, note.text + (f"\n\ntags: {tags}" if tags else ""), note.mtime, save=save)

    # --------------------------------------------------------------- notes
    def all(self, tag: str | None = None) -> list[Note]:
        notes = [self.get(nid) for nid in sorted(self._files())]
        if tag:
            tag = tag.lstrip("#").lower()
            notes = [n for n in notes if tag in n.tags]
        return notes

    def get(self, note_id) -> Note:
        nid = parse_id(note_id)
        p = self._path(nid)
        if not p.is_file():
            raise NotFound(f"no note {nid}")
        return Note(nid, p.read_text(encoding="utf-8", errors="replace"), p.stat().st_mtime)

    def _write(self, nid: int, text: str) -> Note:
        text = text.strip() + "\n"
        if not text.strip():
            raise Invalid("a note needs some text")
        p = self._path(nid)
        p.write_text(text, encoding="utf-8")
        note = Note(nid, text, p.stat().st_mtime)
        self._index(note)
        return note

    def add(self, text: str) -> Note:
        if not text.strip():
            raise Invalid("a note needs some text")
        nid = max(self._files(), default=0) + 1
        # Claim the number without overwriting a note another process just saved.
        while True:
            try:
                with open(self._path(nid), "x", encoding="utf-8"):
                    break
            except FileExistsError:
                nid += 1
        return self._write(nid, text)

    def update(self, note_id, text: str) -> Note:
        return self._write(self.get(note_id).id, text)

    def append(self, note_id, text: str) -> Note:
        note = self.get(note_id)
        return self._write(note.id, note.text.rstrip() + "\n\n" + text.strip())

    def delete(self, note_id) -> Note:
        note = self.get(note_id)
        self._path(note.id).unlink()
        self.rag.remove(note.source)
        return note

    def import_cag(self, directory: Path) -> int:
        """CAG files from an earlier version become notes; the folder is renamed so this
        happens once."""
        n = 0
        for f in sorted(directory.glob("*.md")):
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            first = next((ln for ln in text.splitlines() if ln.strip()), "")
            if f.stem.lower() not in first.lower():
                text = f"# {f.stem}\n\n{text}"     # keep the file's name as the note's title
            self.add(text)
            n += 1
        directory.rename(directory.with_name(directory.name + ".imported"))
        return n

    def tags(self) -> Counter:
        counts: Counter = Counter()
        for n in self.all():
            counts.update(n.tags)
        return counts

    def search(self, query: str, tag: str | None = None, k: int = 5) -> list[tuple[Note, Chunk]]:
        """Notes matching `query` (keywords, plus meaning when embeddings are on), best
        first, one hit per note. With `tag`, only notes carrying it."""
        tag = tag.lstrip("#").lower() if tag else None
        out: dict[int, tuple[Note, Chunk]] = {}
        for ch in self.rag.search(query, k=max(k * 6, 30), min_relative=0.2):
            if not ch.source.startswith(SOURCE) or not ch.source[len(SOURCE):].isdigit():
                continue
            nid = int(ch.source[len(SOURCE):])
            if nid in out:
                continue
            try:
                note = self.get(nid)
            except NotFound:
                continue
            if tag and tag not in note.tags:
                continue
            ch.text = _TAGS_LINE.sub("", ch.text)
            out[nid] = (note, ch)
            if len(out) >= k:
                break
        return list(out.values())


def open_notes(cfg) -> NoteStore:
    """The workspace's notes, without a Harness (note mode loads no chat model). Notes go
    into the project RAG index, the same one /index fills, embedded as configured."""
    from .engines import make_embedder_for
    rag = RagStore(cfg.state.project_rag, make_embedder_for(cfg))
    return NoteStore.for_workspace(cfg.state, rag)
