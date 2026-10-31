"""The harness's own folder in a workspace, `.smallharness/`, and what is kept there.
Every path into it is named here; each folder is created the first time it is used."""
from __future__ import annotations

import time
from pathlib import Path


class StateDir:
    def __init__(self, root: Path):
        self.root = root
        self._made: set[str] = set()

    def _dir(self, name: str) -> Path:
        p = self.root / name
        if name not in self._made:
            p.mkdir(parents=True, exist_ok=True)
            self._made.add(name)
        return p

    @property
    def sessions(self) -> Path:
        return self._dir("sessions")

    @property
    def rag(self) -> Path:
        return self._dir("rag")

    @property
    def plans(self) -> Path:
        return self._dir("plans")

    @property
    def research(self) -> Path:
        """Each research session's bibliography (temporary: removed with old sessions)."""
        return self._dir("research")

    @property
    def library(self) -> Path:
        """Files added to the knowledge base from the web page."""
        return self._dir("library")

    @property
    def chats(self) -> Path:
        """Chat mode's conversations, each with its archive of folded turns (chat.py)."""
        return self._dir("chats")

    @property
    def notes(self) -> Path:
        return self._dir("notes")

    @property
    def relics(self) -> Path:
        """The files plans wrote (relics.py)."""
        self._dir("")
        return self.root / "relics.json"

    @property
    def tools(self) -> Path:
        """Drop-in tools that belong to this workspace."""
        return self._dir("tools")

    @property
    def project_rag(self) -> Path:
        return self.rag / "project.sqlite"

    def session_rag(self, session_id: str) -> Path:
        return self.rag / f"session-{session_id}.sqlite"

    def prune_session_indexes(self, keep_days: int, current: str = "") -> int:
        """Remove session indexes whose session is gone, and those not used for
        `keep_days` days (0: only the orphans). The current session's is kept."""
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for db in [*self.rag.glob("session-*.sqlite"), *self.rag.glob("session-*.json")]:   # .json: older versions
            sid = db.stem[len("session-"):]
            orphan = not (self.sessions / f"{sid}.jsonl").exists()
            stale = keep_days > 0 and db.stat().st_mtime < cutoff
            if sid != current and (orphan or stale):
                for f in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"),
                          db.with_suffix(".json.imported")):
                    f.unlink(missing_ok=True)
                removed += 1
        for bib in self.research.glob("*.md"):            # research bibliographies
            if keep_days > 0 and bib.stat().st_mtime < cutoff:
                bib.unlink(missing_ok=True)
                removed += 1
        return removed

    @property
    def history(self) -> Path:
        """The terminal's input history."""
        self._dir("")
        return self.root / "history"
