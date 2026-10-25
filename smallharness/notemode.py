"""Note mode: a terminal note-taking interface that loads no model.

Started with `smallharness notes`, or `/notes` from a chat (`/exit` goes back).
Typing a line saves it as a note; the commands below list, search, edit and tag.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time

from . import ui
from .errors import HarnessError
from .notes import Note, NoteStore

HELP = [
    ("TEXT", "save TEXT as a new note (end a line with \\ to continue it)"),
    ("/new", "write a longer note: in $EDITOR if set, else line by line ending with a lone ."),
    ("/list [#tag]", "list notes, optionally only those with a tag"),
    ("/show N", "show note N"),
    ("/search WORDS [#tag]", "search notes by keywords (and meaning, when embeddings are on)"),
    ("/tags", "every tag and how many notes have it"),
    ("/edit N", "edit note N (in $EDITOR if set, else retype it)"),
    ("/append N TEXT", "add TEXT to the end of note N"),
    ("/delete N", "delete note N"),
    ("/help", "show this help"),
    ("/exit", "leave note mode"),
]


def _when(mtime: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))


def _tag_list(note: Note) -> str:
    return " ".join(ui.sky("#" + t) for t in note.tags)


def _split_tag(words: list[str]) -> tuple[str, str | None]:
    tag = next((w for w in words if w.startswith("#") and len(w) > 1), None)
    return " ".join(w for w in words if w != tag), tag


def write_text(initial: str = "", what: str = "note") -> str | None:
    """Long text: in the user's editor if one is set, else typed line by line."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(initial)
            path = f.name
        try:
            subprocess.run([*shlex.split(editor), path], check=False)
            with open(path, encoding="utf-8") as f:
                text = f.read()
        finally:
            os.unlink(path)
        return text if text.strip() and text != initial else None
    if initial:
        print(ui.dim(initial))
    print(ui.dim(f"  type the {what}; a line with only . ends it, an empty {what} cancels"))
    lines = []
    while True:
        try:
            ln = input(ui.dim("  | "))
        except EOFError:
            break
        if ln.strip() == ".":
            break
        lines.append(ln)
    text = "\n".join(lines)
    return text if text.strip() else None


class NoteMode:
    def __init__(self, store: NoteStore, inside_chat: bool = False):
        self.store = store
        self.inside_chat = inside_chat

    def banner(self) -> None:
        n = len(self.store.all())
        back = "back to the chat" if self.inside_chat else "quit"
        print(ui.box([
            ui.accent(ui.bold("Note mode")) + ui.dim("  · no model loaded"),
            "",
            f"  {ui.steel('notes')}  {n} in {self._where()}",
            "",
            ui.dim(f"  type to save a note · #tags optional · /help · /exit to {back}"),
        ], color=ui.deep))

    def _where(self) -> str:
        d = self.store.dir
        return f".smallharness/{d.name}" if d.parent.name == ".smallharness" else str(d)

    def run(self) -> None:
        self.banner()
        while True:
            try:
                line = self._read()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line.strip():
                continue
            try:
                if line.startswith("/"):
                    if self.command(line) is False:
                        return
                else:
                    self.save(line)
            except (HarnessError, ValueError) as e:
                print(ui.err(str(e)))

    def _read(self) -> str:
        print()
        lines = [input(ui.accent(ui.bold("note › ")))]
        while lines[-1].endswith("\\"):
            lines[-1] = lines[-1][:-1]
            lines.append(input(ui.dim("     . ")))
        return "\n".join(lines)

    def command(self, line: str):
        name, _, arg = line[1:].partition(" ")
        fn = getattr(self, f"cmd_{name}", None)
        if fn is None:
            print(ui.err(f"unknown command /{name}; try /help"))
            return None
        return fn(arg.strip())

    # ------------------------------------------------------------ actions
    def save(self, text: str) -> None:
        note = self.store.add(text)
        tags = f"  {_tag_list(note)}" if note.tags else ""
        print(ui.ok(f"  ✓ saved [{note.id}] {note.title}") + tags)

    def _write(self, initial: str = "") -> str | None:
        return write_text(initial)

    def cmd_new(self, arg):
        text = arg or self._write()
        if text is None:
            print(ui.dim("  nothing saved"))
            return
        self.save(text)

    def cmd_list(self, arg):
        notes = self.store.all(arg or None)
        if not notes:
            print(ui.dim("  no notes" + (f" tagged {arg}" if arg else " yet: type something to save one")))
            return
        w = len(str(notes[-1].id))
        for n in notes:
            print(f"  {ui.steel(f'[{n.id:>{w}}]')} {n.title}  {_tag_list(n)}  {ui.dim(_when(n.mtime))}")

    def cmd_show(self, arg):
        n = self.store.get(arg)
        head = f"[{n.id}] · {_when(n.mtime)}" + (f" · {' '.join('#' + t for t in n.tags)}" if n.tags else "")
        print(ui.box(n.text.rstrip().splitlines() or [""], head, ui.deep))

    def cmd_search(self, arg):
        query, tag = _split_tag(arg.split())
        if not query:
            self.cmd_list(tag or "")
            return
        hits = self.store.search(query, tag)
        if not hits:
            print(ui.dim(f"  no notes match {query!r}" + (f" with {tag}" if tag else "")))
            return
        for note, chunk in hits:
            snippet = " ".join(chunk.text.split())[:110]
            print(f"  {ui.steel(f'[{note.id}]')} {ui.bold(note.title)}  {_tag_list(note)}")
            print(f"       {ui.dim(snippet)}")

    def cmd_tags(self, _):
        counts = self.store.tags()
        if not counts:
            print(ui.dim("  no tags yet: write #word in a note"))
            return
        print("  " + "  ".join(f"{ui.sky('#' + t)} {ui.dim(str(c))}" for t, c in counts.most_common()))

    def cmd_edit(self, arg):
        note = self.store.get(arg)
        text = self._write(note.text)
        if text is None:
            print(ui.dim("  unchanged"))
            return
        note = self.store.update(note.id, text)
        print(ui.ok(f"  ✓ updated [{note.id}] {note.title}"))

    def cmd_append(self, arg):
        nid, _, text = arg.partition(" ")
        if not text.strip():
            raise ValueError("usage: /append N TEXT")
        note = self.store.append(nid, text)
        print(ui.ok(f"  ✓ added to [{note.id}] {note.title}"))

    def cmd_delete(self, arg):
        note = self.store.get(arg)
        if ui.confirm(f"Delete [{note.id}] {note.title}?", tty_only=False):
            self.store.delete(note.id)
            print(ui.ok("  ✓ deleted"))
        else:
            print(ui.dim("  kept"))

    def cmd_help(self, _):
        w = max(len(c) for c, _ in HELP)
        for c, d in HELP:
            print(f"  {ui.sky(c.ljust(w))}  {ui.dim(d)}")

    def cmd_exit(self, _):
        return False

    cmd_quit = cmd_back = cmd_exit
