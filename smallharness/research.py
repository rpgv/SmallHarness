"""Research mode: a knowledge base of files (text, PDFs and images, read with OCR) and
notes, searched by the harness and answered from by the model.

- Everything is indexed into the project RAG index when it is added; adding may take a
  while (OCR, embeddings) so that asking is fast.
- Each question is answered in a clean context: a one-line system prompt, the numbered
  excerpts the search found, any answers you attached, and the question. No
  conversation history, no tools.
- Excerpts keep one number for the whole research session, so the answer's [n] marks
  point into the session's bibliography (`.smallharness/research/<id>.md`), which
  lists each source, line and excerpt.
- Attach an answer to keep it in the context of later questions (CAG); detach it to
  take it out. Questions can be limited to some files (the scope)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import prompts
from .context.rag import IndexResult, search
from .errors import Invalid, NotFound
from .notes import SOURCE as NOTE_SOURCE

if TYPE_CHECKING:
    from .agent import Events, Harness

_CITE = re.compile(r"\[(\d+)\]")


@dataclass
class Reference:
    num: int
    source: str
    start_line: int
    text: str

    def cite(self) -> str:
        return f"{self.source}:{self.start_line}"


@dataclass
class Answer:
    num: int
    question: str
    text: str
    refs: list[int]                       # the references it rests on
    scope: list[str] = field(default_factory=list)
    attached: bool = False

    def to_dict(self) -> dict:
        return {"num": self.num, "question": self.question, "text": self.text, "refs": self.refs,
                "scope": self.scope, "attached": self.attached}


class Research:
    def __init__(self, h: Harness, session_id: str | None = None):
        self.h = h
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S")
        self.refs: list[Reference] = []
        self._ref_nums: dict[tuple[str, int], int] = {}
        self.answers: list[Answer] = []
        self.scope: list[str] = []            # sources (files, folders, notes); empty = everything

    @property
    def settings(self) -> dict:
        return self.h.cfg["research"]

    @property
    def bibliography_path(self) -> Path:
        return self.h.cfg.state.research / f"{self.id}.md"

    # ------------------------------------------------------------ library
    def library(self) -> list[dict]:
        """Every file and note in the knowledge base, with its chunk count."""
        counts: dict[str, int] = {}
        for c in self.h.project_rag.chunks:
            counts[c["source"]] = counts.get(c["source"], 0) + 1
        notes = {n.source: n for n in self.h.notes.all()}
        out = []
        for source in sorted(self.h.project_rag.sources):
            note = notes.get(source)
            if source.startswith(NOTE_SOURCE) and not note:
                continue
            out.append({"source": source, "kind": "note" if note else "file", "chunks": counts.get(source, 0),
                        "title": note.title if note else source, "in_scope": self.in_scope(source)})
        return out

    def add(self, path: str, progress=None) -> IndexResult:
        """Index a file or folder of the workspace (text, PDFs, images)."""
        try:
            p = self.h.tool_context().resolve(path)
        except PermissionError as e:
            raise Invalid(str(e)) from None
        if not p.exists():
            raise NotFound(f"{path} does not exist")
        return self.h.project_rag.add_path(p, self.h.cfg.workspace, self.h.cfg["rag"], progress)

    def add_upload(self, name: str, data: bytes) -> IndexResult:
        """A file sent from the web page: kept in `.smallharness/library/`, then indexed."""
        name = Path(name).name
        if not name or name.startswith("."):
            raise Invalid("a file needs a name")
        dest = self.h.cfg.state.library / name
        dest.write_bytes(data)
        return self.h.project_rag.add_path(dest, self.h.cfg.workspace, self.h.cfg["rag"])

    def remove(self, source: str) -> None:
        """Take a file or note out of the knowledge base (an uploaded file is deleted)."""
        if source.startswith(NOTE_SOURCE):
            self.h.notes.delete(source[len(NOTE_SOURCE):])
            return
        if source not in self.h.project_rag.sources:
            raise NotFound(f"{source} is not in the knowledge base")
        self.h.project_rag.remove(source)
        uploaded = self.h.cfg.workspace / source
        if uploaded.parent == self.h.cfg.state.library:
            uploaded.unlink(missing_ok=True)
        self.scope = [s for s in self.scope if s != source]

    # -------------------------------------------------------------- scope
    def set_scope(self, sources: list[str]) -> None:
        """Limit questions to these files, folders or notes; [] for everything."""
        known = self.h.project_rag.sources
        for s in sources:
            if not any(k == s or k.startswith(s.rstrip("/") + "/") for k in known):
                raise NotFound(f"{s} is not in the knowledge base")
        self.scope = list(sources)

    def in_scope(self, source: str) -> bool:
        return not self.scope or any(source == s or source.startswith(s.rstrip("/") + "/") for s in self.scope)

    # ------------------------------------------------------------- asking
    def _ref(self, source: str, start_line: int, text: str) -> int:
        key = (source, start_line)
        if key not in self._ref_nums:
            self.refs.append(Reference(len(self.refs) + 1, source, start_line, text))
            self._ref_nums[key] = len(self.refs)
        return self._ref_nums[key]

    def find(self, query: str, k: int | None = None) -> list[Reference]:
        """The excerpts for `query` (no model), numbered into the bibliography."""
        hits = search([self.h.project_rag], query, k or self.settings["k"],
                      where=self.in_scope if self.scope else None)
        refs = [self.refs[self._ref(c.source, c.start_line, c.text) - 1] for c in hits]
        self._write_bibliography()
        return refs

    def ask(self, question: str, events: Events | None = None) -> Answer:
        question = question.strip()
        if not question:
            raise Invalid("ask a question")
        refs = self.find(question)
        num = len(self.answers) + 1
        if not refs:
            answer = Answer(num, question, "Nothing in the knowledge base matches this question"
                            + (" (within the scope)." if self.scope else "."), [], list(self.scope))
            self.answers.append(answer)
            return answer
        budget = self.h.cfg.context_tokens - self.h.cfg["model"]["max_output_tokens"]
        count = self.h.counter
        excerpts, used = [], 0
        for r in refs:
            part = f"[{r.num}] ({r.source})\n{r.text.strip()}"
            cost = count(part)
            if used + cost > budget * self.settings["excerpt_budget"] and excerpts:
                break
            excerpts.append(part)
            used += cost
        given = [r.num for r in refs[:len(excerpts)]]
        attached = [a for a in self.answers if a.attached]
        attached_text = ""
        if attached:
            body = "\n\n".join(f"Q: {a.question}\nA: {_plain(a.text)}" for a in attached)
            attached_text = prompts.ATTACHED.format(
                answers=count.truncate(body, int(budget * self.settings["attached_budget"])))
        prompt = prompts.RESEARCH.format(attached=attached_text, excerpts="\n\n".join(excerpts), question=question)
        reply = self.h.generate_clean(prompt, prompts.RESEARCH_SYSTEM, stream=events is not None, events=events)
        text = _CITE.sub(lambda m: m.group(0) if int(m.group(1)) in given else "", reply).strip()
        cited = sorted({int(n) for n in _CITE.findall(text)})
        answer = Answer(num, question, text, cited or given, list(self.scope))
        self.answers.append(answer)
        return answer

    # ---------------------------------------------------------- attaching
    def answer(self, num: int) -> Answer:
        if not 1 <= num <= len(self.answers):
            raise NotFound(f"no answer {num}")
        return self.answers[num - 1]

    def attach(self, num: int) -> Answer:
        a = self.answer(num)
        a.attached = True
        return a

    def detach(self, num: int) -> Answer:
        a = self.answer(num)
        a.attached = False
        return a

    # -------------------------------------------------------- bibliography
    def bibliography(self) -> str:
        lines = [f"# References · research {self.id}", ""]
        for r in self.refs:
            excerpt = " ".join(r.text.split())
            lines += [f"[{r.num}] {r.cite()}", f"> {excerpt[:600]}{'…' if len(excerpt) > 600 else ''}", ""]
        return "\n".join(lines)

    def _write_bibliography(self) -> None:
        self.bibliography_path.write_text(self.bibliography() + "\n", encoding="utf-8")

    def to_dict(self) -> dict:
        return {"id": self.id, "scope": self.scope, "answers": [a.to_dict() for a in self.answers],
                "references": [{"num": r.num, "source": r.source, "start_line": r.start_line, "text": r.text}
                               for r in self.refs],
                "bibliography": str(self.bibliography_path)}


def _plain(text: str) -> str:
    """An answer without its citation marks, for the context of later questions."""
    return " ".join(_CITE.sub("", text).split())
