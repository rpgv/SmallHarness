"""The code library: every section a recipe writes in code mode is kept as its own
file, indexed with a brief description, one library per programming language (from
the recipe target's extension). Recipes are assembled from these files, and the
library is searched before a section is written, so code that already exists is
reused instead of being generated again.

    .smallharness/snippets/<language>/
        <id><ext>        one snippet: the code a section produced
        manifest.json    id -> description, the section text it was written for, recipes
        index.sqlite     search index over descriptions, section texts and code

A snippet's id is its first name (function, class) and a hash of its code, so the
same code is stored once whichever recipe wrote it."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .context.rag import RagStore, search, terms
from .errors import NotFound

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,80}")
_DEFINED = re.compile(r"^\s*(?:async\s+def|def|class|function|func|fn|struct|interface|enum|type)\s+([A-Za-z_]\w*)",
                      re.M)


def language_key(language: str) -> str:
    """'Python' -> 'python', 'C++' -> 'cpp', 'C#' -> 'csharp': the library's folder."""
    return re.sub(r"[^a-z0-9]+", "", language.lower().replace("++", "pp").replace("#", "sharp")) or "code"


@dataclass
class Snippet:
    id: str
    description: str               # brief: the first line of the section it was written for
    section: str                   # the whole recipe section
    recipes: list[str] = field(default_factory=list)
    created: float = 0.0

    def to_dict(self, code: str = "") -> dict:
        return dict(asdict(self), code=code)


def similarity(a: str, b: str) -> float:
    """How alike two section texts are: shared searchable words over all of them (0..1)."""
    ta, tb = set(terms(a)), set(terms(b))
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


class Library:
    def __init__(self, root: Path, language: str, ext: str, embedder=None):
        self.language, self.ext = language, ext
        self.dir = root / language_key(language)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._manifest = self.dir / "manifest.json"
        self._lock = threading.Lock()
        self.index = RagStore(self.dir / "index.sqlite", embedder)

    # ---------------------------------------------------------------- reading
    def _read(self) -> dict[str, dict]:
        try:
            return json.loads(self._manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def all(self) -> list[Snippet]:
        return [Snippet(**v) for v in self._read().values() if (self.dir / f"{v['id']}{self.ext}").is_file()]

    def get(self, snippet_id: str) -> Snippet:
        data = self._read().get(snippet_id)
        if not data or not self.path(snippet_id).is_file():
            raise NotFound(f"no {self.language} snippet {snippet_id!r}")
        return Snippet(**data)

    def path(self, snippet_id: str) -> Path:
        return self.dir / f"{snippet_id}{self.ext}"

    def code(self, snippet_id: str) -> str:
        return self.path(snippet_id).read_text(encoding="utf-8")

    def search(self, text: str, k: int = 3, skip: set[str] = frozenset()) -> list[Snippet]:
        """The snippets most like `text` (a section), best first; none written for a
        section whose text is in `skip`."""
        known = self._read()
        hits = search([self.index], text, (k + len(skip)) * 2, min_relative=0.2)
        out, seen = [], set()
        for h in hits:
            entry = known.get(h.source)
            if entry and h.source not in seen and entry["section"] not in skip and self.path(h.source).is_file():
                seen.add(h.source)
                out.append(Snippet(**entry))
        return out[:k]

    def match(self, section: str, threshold: float, skip: set[str] = frozenset()) -> Snippet | None:
        """A snippet written for a section that says nearly the same thing, if any."""
        best = max(((similarity(section, s.section), s) for s in self.search(section, 5, skip)),
                   key=lambda x: x[0], default=(0.0, None))
        return best[1] if best[0] >= threshold else None

    # ---------------------------------------------------------------- writing
    def add(self, code: str, section: str, recipe: str) -> Snippet:
        """Keep `code` (what `section` of `recipe` produced) as a snippet; the same code
        is kept once, and remembers every recipe that used it."""
        code = code.strip("\n") + "\n"
        name = (_DEFINED.findall(code) or [w for w in re.findall(r"[A-Za-z_]\w*", section)][:1] or ["snippet"])[0]
        sid = f"{re.sub(r'[^a-z0-9_]+', '-', name.lower()).strip('-') or 'snippet'}-" \
              f"{hashlib.sha1(code.encode()).hexdigest()[:8]}"
        with self._lock:
            manifest = self._read()
            entry = manifest.get(sid)
            if entry:
                if recipe not in entry["recipes"]:
                    entry["recipes"].append(recipe)
            else:
                description = next((ln.strip() for ln in section.splitlines() if ln.strip()), name)[:100]
                entry = asdict(Snippet(sid, description, section, [recipe], time.time()))
                manifest[sid] = entry
                self.path(sid).write_text(code, encoding="utf-8")
                signatures = "\n".join(_DEFINED.findall(code))
                self.index.add_text(sid, f"{description}\n{section}\n{signatures}\n{code}")
            self._manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return Snippet(**entry)

    def used_by(self, snippet_id: str, recipe: str) -> None:
        with self._lock:
            manifest = self._read()
            if snippet_id in manifest and recipe not in manifest[snippet_id]["recipes"]:
                manifest[snippet_id]["recipes"].append(recipe)
                self._manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def remove(self, snippet_id: str) -> None:
        with self._lock:
            manifest = self._read()
            if snippet_id not in manifest:
                raise NotFound(f"no {self.language} snippet {snippet_id!r}")
            del manifest[snippet_id]
            self._manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            self.path(snippet_id).unlink(missing_ok=True)
            self.index.remove(snippet_id)

    def close(self) -> None:
        self.index.close()


def libraries(cfg) -> list[str]:
    """The languages that have a library."""
    root = cfg.state.snippets
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "manifest.json").is_file())


def locate(target_or_key: str) -> tuple[str, str] | None:
    """(language, file extension) for a recipe target (by its extension) or a language
    key such as "python"; None for a file type that is not code. Each language keeps
    one extension for its snippets, whichever of its extensions the target has."""
    from .recipe import LANGUAGES
    suffix = Path(target_or_key).suffix.lower()
    if suffix in LANGUAGES:
        lang = LANGUAGES[suffix][0]
    else:
        lang = next((lg for lg, _ in LANGUAGES.values() if language_key(lg) == target_or_key.lower()), None)
    if lang is None:
        return None
    return lang, next(e for e, (lg, _) in LANGUAGES.items() if lg == lang)


def snippet_path(cfg, target: str, snippet_id: str) -> Path | None:
    """Where a snippet's file is, without opening the library's index."""
    found = locate(target)
    return cfg.state.snippets / language_key(found[0]) / f"{snippet_id}{found[1]}" if found else None


def open_library(cfg, target_or_key: str, embedder=None) -> Library | None:
    """The library for a recipe target or a language key; None if it is not code."""
    found = locate(target_or_key)
    return Library(cfg.state.snippets, *found, embedder) if found else None
