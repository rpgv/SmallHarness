"""Session context: the harness-side record of everything that happened.

The *session* keeps the full history (persisted as JSONL) plus what the model
currently sees: a rolling summary, the turns still in the window, and context
the user or model explicitly added. Turns pushed out of the window on refresh
are archived into the session's RAG index, so they stay retrievable."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import Invalid, NotFound


@dataclass
class Turn:
    n: int
    role: str                   # user | assistant | tool
    content: str
    ts: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)


@dataclass
class Injected:
    label: str                  # e.g. "file:src/app.py" or "rag:how auth works"
    text: str


class Session:
    def __init__(self, sessions_dir: Path | None, session_id: str | None = None):
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.log_path = sessions_dir / f"{self.id}.jsonl" if sessions_dir else None
        self.history: list[Turn] = []
        self.window: list[Turn] = []
        self.summary = ""
        self.injected: list[Injected] = []
        self.refreshes = 0

    # ---------------------------------------------------------------- record
    def _log(self, event: dict) -> None:
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(event) + "\n")

    def add(self, role: str, content: str, **meta) -> Turn:
        turn = Turn(len(self.history), role, content, meta=meta)
        self.history.append(turn)
        self.window.append(turn)
        self._log({"type": "turn", **asdict(turn)})
        return turn

    def inject(self, label: str, text: str) -> None:
        self.injected = [i for i in self.injected if i.label != label]
        self.injected.append(Injected(label, text))
        self._log({"type": "inject", "label": label, "text": text})

    def drop_injected(self, label: str | None = None) -> int:
        before = len(self.injected)
        self.injected = [i for i in self.injected if label and i.label != label]
        self._log({"type": "drop", "label": label})
        return before - len(self.injected)

    def compact(self, summary: str, keep_from: int) -> list[Turn]:
        """Replace window turns before history index `keep_from` with `summary`."""
        old = [t for t in self.window if t.n < keep_from]
        self.window = [t for t in self.window if t.n >= keep_from]
        self.summary = summary
        self.refreshes += 1
        self._log({"type": "refresh", "summary": summary, "keep_from": keep_from})
        return old

    def clear_window(self) -> None:
        self.window, self.summary, self.injected = [], "", []
        self._log({"type": "clear"})

    # ---------------------------------------------------------------- resume
    @classmethod
    def load(cls, sessions_dir: Path, session_id: str) -> "Session":
        if not re.fullmatch(r"[\w-]+", session_id or ""):
            raise Invalid(f"bad session id {session_id!r}")
        path = sessions_dir / f"{session_id}.jsonl"
        if not path.exists():
            raise NotFound(f"no session {session_id}")
        s = cls(None, session_id)
        for line in path.read_text().splitlines():
            ev = json.loads(line)
            kind = ev.pop("type")
            if kind == "turn":
                t = Turn(**ev)
                s.history.append(t)
                s.window.append(t)
            elif kind == "inject":
                s.injected = [i for i in s.injected if i.label != ev["label"]] + [Injected(ev["label"], ev["text"])]
            elif kind == "drop":
                s.injected = [i for i in s.injected if ev["label"] and i.label != ev["label"]]
            elif kind == "refresh":
                s.window = [t for t in s.window if t.n >= ev["keep_from"]]
                s.summary = ev["summary"]
                s.refreshes += 1
            elif kind == "clear":
                s.window, s.summary, s.injected = [], "", []
        s.log_path = path
        return s

    @staticmethod
    def list(sessions_dir: Path) -> list[str]:
        return sorted((p.stem for p in sessions_dir.glob("*.jsonl")), reverse=True)


def transcript(turns: list[Turn]) -> str:
    labels = {"user": "USER", "assistant": "ASSISTANT", "tool": "TOOL"}
    return "\n".join(f"{labels.get(t.role, t.role.upper())}: {t.content}" for t in turns)
