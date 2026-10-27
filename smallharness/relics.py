"""Relics: the files recipes and plans made. Each is kept by its path in the workspace,
with what wrote it (`recipe:NAME`, `plan:ID`) and when, so they can be found and
edited again from the web interface. The list lives in `.smallharness/relics.json`;
forgetting a relic leaves its file alone."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .errors import Invalid, NotFound

WRITE_TOOLS = {"risky_write_file"}     # tools whose 'path' argument is a file they wrote


class RelicStore:
    def __init__(self, path: Path, workspace: Path):
        self.path = path
        self.workspace = workspace
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def rel(self, path: str | Path) -> str:
        """The path relative to the workspace; anything outside it is refused."""
        p = (self.workspace / path).resolve()
        if not p.is_relative_to(self.workspace):
            raise Invalid(f"{str(path)!r} is outside the workspace")
        return p.relative_to(self.workspace).as_posix()

    def record(self, path: str | Path, by: str) -> None:
        """Remember that `by` wrote `path`."""
        rel, now = self.rel(path), time.time()
        with self._lock:
            data = self._read()
            entry = data.setdefault(rel, {"by": [], "created": now})
            if by not in entry["by"]:
                entry["by"].append(by)
            entry["updated"] = now
            self._write(data)

    def all(self) -> list[dict]:
        """Every relic, the latest written first, with whether its file is still there."""
        out = []
        for rel, entry in self._read().items():
            p = self.workspace / rel
            out.append({"path": rel, "by": entry.get("by", []), "created": entry.get("created", 0),
                        "updated": entry.get("updated", 0), "exists": p.is_file(),
                        "size": p.stat().st_size if p.is_file() else 0})
        return sorted(out, key=lambda r: r["updated"], reverse=True)

    def _known(self, path: str) -> Path:
        rel = self.rel(path)
        if rel not in self._read():
            raise NotFound(f"{path} is not a relic")
        return self.workspace / rel

    def read(self, path: str) -> str:
        p = self._known(path)
        if not p.is_file():
            raise NotFound(f"{path} is gone from the workspace")
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise Invalid(f"{path} is not a text file") from None

    def save(self, path: str, text: str) -> None:
        """Edited by hand: the file is written as given."""
        p = self._known(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        self.record(path, "edited")

    def forget(self, path: str) -> None:
        self._known(path)
        with self._lock:
            data = self._read()
            data.pop(self.rel(path), None)
            self._write(data)
