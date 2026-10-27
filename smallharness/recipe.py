"""Recipes: pseudo code turned into code, one section at a time.

A recipe is a text file of sections separated by empty lines, each describing the
methods, classes or statements wanted and what they take and return. An optional first
line `target: FILE` names the file to write (or pass it when running).

Each section is written by its own clean-context model call that sees only that
section: no conversation, no retrieval, no other sections. Its only tool is
risky_write_file, and you allow or deny it once, before the first section. Every
section's code goes into its own slot of the one target file, in recipe order (for
Python, imports are gathered at the top), so a section can be written again without
touching the others. The code of each section is kept in a hidden file in the state
folder (`recipes/.<name>.json`); a run writes only sections that are new, changed or
failed, unless asked to write them all."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import prompts
from .errors import Invalid, NotFound
from .steps import back_to_pending
from .toolcall import ToolCallError, parse_tool_call

if TYPE_CHECKING:
    from .agent import Events, Harness

TOOL = "risky_write_file"
_HEADER = re.compile(r"^\s*(target|mode)\s*:\s*(\S.*?)\s*$", re.I)
_BLANK = re.compile(r"\n[ \t]*\n")
_FENCE = re.compile(r"```[\w+-]*[ \t]*\n(.*?)```", re.S)
_NAME = re.compile(r"[A-Za-z0-9][\w-]{0,63}")

# suffix -> (language, line comment)
LANGUAGES = {
    ".py": ("Python", "#"), ".sh": ("shell", "#"), ".rb": ("Ruby", "#"), ".r": ("R", "#"),
    ".pl": ("Perl", "#"), ".jl": ("Julia", "#"), ".ex": ("Elixir", "#"), ".exs": ("Elixir", "#"),
    ".js": ("JavaScript", "//"), ".mjs": ("JavaScript", "//"), ".ts": ("TypeScript", "//"),
    ".tsx": ("TypeScript", "//"), ".jsx": ("JavaScript", "//"), ".java": ("Java", "//"),
    ".kt": ("Kotlin", "//"), ".go": ("Go", "//"), ".rs": ("Rust", "//"), ".c": ("C", "//"),
    ".h": ("C", "//"), ".cpp": ("C++", "//"), ".cc": ("C++", "//"), ".hpp": ("C++", "//"),
    ".cs": ("C#", "//"), ".swift": ("Swift", "//"), ".dart": ("Dart", "//"), ".scala": ("Scala", "//"),
    ".php": ("PHP", "//"), ".lua": ("Lua", "--"), ".sql": ("SQL", "--"), ".hs": ("Haskell", "--"),
}


def language(target: str) -> tuple[str, str]:
    return LANGUAGES.get(Path(target).suffix.lower(), ("code", "#"))


def parse_recipe(text: str) -> tuple[dict[str, str], list[str]]:
    """({"target": …, "mode": …} from header lines at the top, the sections in order)."""
    lines = text.replace("\r\n", "\n").split("\n")
    header: dict[str, str] = {}
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and (m := _HEADER.match(lines[0])):
        header[m.group(1).lower()] = m.group(2)
        lines.pop(0)
    body = "\n".join(lines)
    return header, [s.strip("\n") for s in _BLANK.split(body) if s.strip()]


def default_mode(target: str) -> str:
    """Prose for Markdown and text files, code for everything else."""
    return "text" if Path(target).suffix.lower() in (".md", ".markdown", ".txt", ".rst") else "code"


@dataclass
class Section:
    num: int
    text: str
    status: str = "pending"        # pending | running | done | failed
    code: str = ""
    note: str = ""                 # why it failed, or what was fixed
    key: str = ""                  # hash of target + text when the code was written
    snippet: str = ""              # code mode: its file in the code library (snippets.py)
    reused: bool = False           # taken from the library, not written for this recipe
    redo: bool = False             # unticked by hand: the next run writes it anew


@dataclass
class Recipe:
    name: str
    path: Path
    target: str
    sections: list[Section] = field(default_factory=list)
    cache: Path | None = None      # hidden file with each section's state
    mode: str = "code"             # code | text (Markdown prose)
    snippet_file: Callable[[str], Path | None] | None = None   # snippet id -> its file

    def key(self, s: Section) -> str:
        return hashlib.sha1(f"{self.mode}\n{self.target}\n{s.text}".encode()).hexdigest()[:16]

    def marker(self, text: str) -> str:
        """A comment line in the target file: how a section that is missing or failed shows."""
        if self.mode == "text":
            return f"<!-- {text} -->"
        return f"{language(self.target)[1]} {text}"

    def state(self, s: Section) -> str:
        """pending | running | done | failed | changed: written for an earlier version of
        the section or target. A section whose snippet was taken out of the code
        library is pending again."""
        if s.status in ("done", "failed") and s.key != self.key(s):
            return "changed"
        if s.status == "done" and s.snippet and self.snippet_file:
            path = self.snippet_file(s.snippet)
            if path is not None and not path.is_file():
                return "pending"
        return s.status

    def written(self) -> int:
        return sum(1 for s in self.sections if self.state(s) == "done")

    def stale(self) -> list[Section]:
        """Sections that a run writes by default: anything not written for its current text."""
        return [s for s in self.sections if self.state(s) != "done"]

    def code(self, s: Section) -> str:
        """A section's code: from its file in the code library, when it has one."""
        path = self.snippet_file(s.snippet) if s.snippet and self.snippet_file else None
        if path and path.is_file():
            return path.read_text(encoding="utf-8")
        return s.code

    def assemble(self) -> str:
        """The target file: every section's code in recipe order."""
        python = self.mode == "code" and language(self.target)[0] == "Python"
        blocks, imports = [], []
        for s in self.sections:
            code = self.code(s).strip("\n")
            state = self.state(s)
            if not code or state == "changed":
                failed = state == "failed" and s.note
                blocks.append(self.marker(f"recipe section {s.num} failed: {s.note}" if failed else
                                          f"recipe section {s.num}: not written yet"))
                continue
            if python:
                code, found = _split_imports(code)
                imports += [i for i in found if i not in imports]
            code = code.strip("\n") or self.marker(f"recipe section {s.num}: only imports")
            if s.status == "failed":    # kept so it can be seen and fixed
                code = self.marker(f"recipe section {s.num} failed: {s.note}") + "\n" + code
            blocks.append(code)
        if imports:
            imports.sort(key=lambda i: not i.startswith("from __future__"))
            blocks.insert(0, "\n".join(imports))
        gap = "\n\n\n" if python else "\n\n"
        return gap.join(blocks) + "\n"

    def save_cache(self) -> None:
        data = {"target": self.target, "sections": [asdict(s) for s in self.sections]}
        self.cache.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def to_dict(self) -> dict:
        return {"name": self.name, "path": str(self.path), "target": self.target, "mode": self.mode,
                "sections": [dict(asdict(s), code=self.code(s), state=self.state(s))
                             for s in self.sections]}


def _split_imports(code: str) -> tuple[str, list[str]]:
    """Top-level single-line imports, moved out of a Python block."""
    keep, found = [], []
    for ln in code.split("\n"):
        if re.match(r"(import\s+\S|from\s+\S+\s+import\s+[^(]*$)", ln) and not ln.rstrip().endswith("\\"):
            found.append(ln.rstrip())
        else:
            keep.append(ln)
    return "\n".join(keep).strip("\n"), found


def load_recipe(cfg, path: Path, target: str | None = None, mode: str | None = None) -> Recipe:
    """Read a recipe file and the code kept from earlier runs. `target` and `mode`
    override the file's header lines."""
    text = path.read_text(encoding="utf-8")
    header, texts = parse_recipe(text)
    saved = recipes_dir(cfg)
    if path.parent == saved:
        slug = path.stem
    else:   # a recipe file in the workspace: named after its path
        slug = "file-" + re.sub(r"[^\w.-]+", "_", str(path.relative_to(cfg.workspace)))
    recipe = Recipe(path.stem, path, (target or header.get("target") or "").strip(), cache=saved / f".{slug}.json")
    if not recipe.target:
        raise Invalid(f"{path.name} names no target file: add a first line `target: FILE` or pass one")
    recipe.mode = (mode or header.get("mode") or default_mode(recipe.target)).lower()
    if recipe.mode not in ("code", "text"):
        raise Invalid(f"mode must be code or text, not {recipe.mode!r}")
    from .snippets import snippet_path
    recipe.snippet_file = lambda sid: snippet_path(cfg, recipe.target, sid)
    if not texts:
        raise Invalid(f"{path.name} has no sections")
    recipe.sections = [Section(i, t) for i, t in enumerate(texts, 1)]
    try:
        old = json.loads(recipe.cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    if old.get("target") == recipe.target:
        by_text = {s["text"]: s for s in old.get("sections", []) if isinstance(s, dict)}
        for s in recipe.sections:
            prev = by_text.get(s.text)   # matched by text, so moving a section keeps its code
            if prev and prev.get("key") == recipe.key(s):
                s.status, s.code, s.note, s.key = prev["status"], prev.get("code", ""), prev.get("note", ""), prev["key"]
                s.snippet, s.reused = prev.get("snippet", ""), prev.get("reused", False)
                s.redo = prev.get("redo", False)
    return recipe


def mark_sections(recipe: Recipe, nums: list[int] | None, done: bool) -> None:
    """Untick sections so the next run writes them again (the model writes them anew,
    rather than taking them from the code library), or tick back ones whose code is
    still that of their current text. `nums` None: every section."""
    sections = recipe.sections if nums is None else [s for s in recipe.sections if s.num in nums]
    missing = sorted(set(nums or []) - {s.num for s in sections})
    if missing:
        raise NotFound(f"no section {missing[0]}: the recipe has {len(recipe.sections)}")
    for s in sections:
        if done and recipe.state(s) != "done":
            if not (s.code or s.snippet) or s.key != recipe.key(s):
                raise Invalid(f"section {s.num} has no code for its current text: write it instead")
            s.status, s.redo = "done", False
        elif not done and (s.status != "pending" or not s.redo):
            s.status, s.redo = "pending", True
    recipe.save_cache()


def recipes_dir(h_or_cfg) -> Path:
    cfg = getattr(h_or_cfg, "cfg", h_or_cfg)
    return cfg.state.recipes


def load_saved(cfg, name: str) -> Recipe:
    """A saved recipe by name."""
    path = recipes_dir(cfg) / f"{name}.md"
    if not _NAME.fullmatch(name) or not path.is_file():
        raise NotFound(f"no recipe {name!r}")
    return load_recipe(cfg, path)


def find_recipe(cfg, name_or_path: str) -> Path:
    """A saved recipe by name, else a recipe file in the workspace."""
    arg = name_or_path.strip()
    saved = recipes_dir(cfg) / f"{arg}.md"
    if _NAME.fullmatch(arg) and saved.is_file():
        return saved
    p = (cfg.workspace / arg).resolve()
    if not p.is_relative_to(cfg.workspace):
        raise Invalid(f"{arg!r} is outside the workspace")
    if not p.is_file():
        raise NotFound(f"no recipe {arg!r} (saved recipes: {', '.join(list_recipes(cfg)) or 'none'})")
    return p


def list_recipes(cfg) -> list[str]:
    return sorted(p.stem for p in recipes_dir(cfg).glob("*.md"))


def save_recipe(cfg, name: str, text: str, target: str = "", mode: str = "") -> Path:
    """Save a recipe; `target` and `mode`, when given, replace its header lines."""
    if not _NAME.fullmatch(name):
        raise Invalid("a recipe name is letters, digits, - and _ (up to 64)")
    header, sections = parse_recipe(text)
    if not sections:
        raise Invalid("the recipe has no sections")
    if mode and mode not in ("code", "text"):
        raise Invalid(f"mode must be code or text, not {mode!r}")
    header.update({k: v for k, v in (("target", target), ("mode", mode)) if v})
    if target or mode:
        head = "".join(f"{k}: {header[k]}\n" for k in ("target", "mode") if header.get(k))
        text = head + "\n" + "\n\n".join(sections) + "\n"
    path = recipes_dir(cfg) / f"{name}.md"
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


# ----------------------------------------------------------------- running
@dataclass
class RecipeResult:
    written: list[int]
    failed: list[int]
    reason: str                    # "finished" | "denied" | "nothing to write" | "interrupted"


class RecipeRunner:
    """Writes the recipe's sections into its target file. `only` limits the run to some
    section numbers; `everything` rewrites sections that are already written."""

    def __init__(self, h: Harness, recipe: Recipe, events: Events | None = None,
                 only: list[int] | None = None, everything: bool = False, retries: int = 1):
        self.h, self.recipe = h, recipe
        self.events = events or h.events
        self.retries = retries
        if only:
            bad = [n for n in only if not 1 <= n <= len(recipe.sections)]
            if bad:
                raise Invalid(f"no section {bad[0]}: the recipe has {len(recipe.sections)}")
            self.todo = [s for s in recipe.sections if s.num in only]
        else:
            self.todo = list(recipe.sections) if everything else recipe.stale()
        self.tool = h.tools.get(TOOL)
        if self.tool is None:
            raise Invalid(f"{TOOL} is not available")
        self.ctx = h.tool_context()
        try:
            self.path = self.ctx.resolve(recipe.target)
        except PermissionError as e:
            raise Invalid(str(e)) from None
        self.library = None
        if recipe.mode == "code" and h.cfg["recipe"]["library"]:
            from .snippets import open_library
            self.library = open_library(h.cfg, recipe.target, h.embedder)

    # ------------------------------------------------------------ permission
    def request(self) -> dict:
        """What the one permission covers, shown before the first section."""
        r = self.recipe
        nums = ", ".join(str(s.num) for s in self.todo)
        args = {"path": r.target,
                "recipe": f"{r.name}: {len(r.sections)} section(s); this run writes {len(self.todo)} ({nums})",
                "asked once": f"each section's code is written into {r.target} with {TOOL}; "
                              "the model gets no other tool"}
        if self.path.exists() and not r.cache.is_file():
            args["overwrites"] = f"{r.target} exists and was not written by this recipe"
        return args

    # ----------------------------------------------------------------- run
    def run(self, permit: Callable[[], bool] | None = None,
            on_start: Callable[[Section], None] | None = None,
            on_end: Callable[[Section], None] | None = None, parallel: bool = False) -> RecipeResult:
        """Write the sections; with `parallel`, several at once on as many model instances
        as RAM allows (each still sees only its own section)."""
        if not self.todo:
            return RecipeResult([], [], "nothing to write")
        allowed = permit() if permit else self.events.permit(self.tool, self.request())
        if not allowed:
            return RecipeResult([], [], "denied")
        from .agent import ForwardingEvents
        written, failed = [], []
        lock = threading.Lock()
        quiet = ForwardingEvents(self.events, drop=("on_text", "on_text_end", "on_stats")) if parallel else None

        def one(s: Section) -> None:
            s.status = "running"
            if on_start:
                on_start(s)
            self.write_section(s, quiet)
            with lock:
                (written if s.status == "done" else failed).append(s.num)
                self._write_file()
                if on_end:
                    on_end(s)
        try:
            if parallel:
                self.h.pool.parallel(one, self.todo)
            else:
                for s in self.todo:
                    one(s)
        except KeyboardInterrupt:
            back_to_pending(self.recipe.sections)
            self.recipe.save_cache()
            return RecipeResult(written, failed, "interrupted")
        return RecipeResult(sorted(written), sorted(failed), "finished")

    def write_section(self, s: Section, events: Events | None = None) -> None:
        """Code mode: first the code library, for this language. A snippet written for a
        section that says nearly the same is reused with no model call; similar ones are
        offered to the model, which may reuse one. Otherwise one clean-context call (plus
        retries) that sees only this section, and new code joins the library."""
        r, lib, cfg = self.recipe, self.library, self.h.cfg["recipe"]
        lang = language(r.target)[0] if r.mode == "code" else "Markdown"
        offered: dict[str, str] = {}
        if lib:
            # Never this recipe's other sections: each section sees only itself, and the
            # file would hold the same code twice.
            siblings = {x.text for x in r.sections if x is not s}
            same = None if s.redo else lib.match(s.text, cfg["reuse_similarity"], siblings)
            if same:
                self._reuse(s, same.id, f"reused {same.id}: written for a section like this one")
                return
            offered = {sn.id: sn.description for sn in lib.search(s.text, cfg["library_k"], siblings)
                       if not (s.redo and sn.id == s.snippet)}
        library = prompts.LIBRARY.format(snippets="\n\n".join(
            f"[{sid}] {about}\n{_head(lib.code(sid))}" for sid, about in offered.items())) if offered else ""
        prompt = prompts.RECIPE[r.mode].format(target=r.target, num=s.num, total=len(r.sections), section=s.text,
                                               library=library)
        system = prompts.RECIPE_SYSTEM[r.mode].format(language=lang)
        history: list[tuple[str, str]] = []
        ask, problem = prompt, ""
        for _attempt in range(self.retries + 1):
            reply = self.h.generate_clean(ask, system, history, stream=True, events=events or self.events)
            chosen = _REUSE.match(reply)
            if chosen and chosen.group(1) in offered:
                self._reuse(s, chosen.group(1), f"reused {chosen.group(1)}: chosen from the library")
                return
            code, problem = self._code_from(reply)
            if code is not None and r.mode == "code" and lang == "Python":
                problem = _python_error(code, r.target)
            if code is not None and not problem:
                s.status, s.code, s.note, s.key, s.reused, s.redo = "done", code, "", r.key(s), False, False
                s.snippet = lib.add(code, s.text, r.name).id if lib else ""
                return
            history += [("user", ask), ("assistant", reply.strip()[:4000])]
            ask = f"{problem} Reply again with only the {TOOL} call for SECTION {s.num}."
        s.status, s.note, s.key = "failed", problem, r.key(s)
        if code is not None:          # keep what it wrote, so you can see and fix it
            s.code = code

    def _reuse(self, s: Section, snippet_id: str, note: str) -> None:
        self.library.used_by(snippet_id, self.recipe.name)
        s.status, s.code, s.note, s.key = "done", self.library.code(snippet_id), note, self.recipe.key(s)
        s.snippet, s.reused, s.redo = snippet_id, True, False

    def _code_from(self, reply: str) -> tuple[str | None, str]:
        """The code from the model's write call, or from a lone code block."""
        try:
            call = parse_tool_call(reply, {TOOL: ["path", "content", "find"]})
        except ToolCallError as e:
            call, err = None, f"The tool call could not be read ({e})."
        else:
            err = "There was no tool call."
        if call is not None:
            if call.name != TOOL:
                return None, f"{call.name} is not available; the only tool is {TOOL}."
            args = call.args
            path = str(args.get("path") or "").strip() or self.recipe.target
            if os.path.normpath(path) != os.path.normpath(self.recipe.target):
                return None, f"This recipe writes only {self.recipe.target}, not {path}."
            if args.get("find"):
                return None, "Do not use 'find': write this section's code as 'content'."
            content = args.get("content")
            if not isinstance(content, str) or not content.strip():
                return None, "The call had no code in 'content'."
            return _unfence(content), ""
        blocks = _FENCE.findall(reply)
        if len(blocks) == 1 and blocks[0].strip():
            return blocks[0].strip("\n"), ""
        return None, err

    def _write_file(self) -> None:
        """The whole target file, written through the allowed tool."""
        self.recipe.save_cache()
        outcome = self.h.tools.execute(TOOL, {"path": self.recipe.target, "content": self.recipe.assemble()},
                                       self.ctx, lambda tool, args: True)
        if not outcome.ok:
            raise RuntimeError(outcome.text)
        self.h.relics.record(self.recipe.target, f"recipe:{self.recipe.name}")


_REUSE = re.compile(r"^\s*REUSE\s+\[?([\w-]+)\]?\s*$")


def _head(code: str, lines: int = 8) -> str:
    """The start of a snippet, for the model to recognise it."""
    rows = code.strip("\n").splitlines()
    return "\n".join(rows[:lines]) + ("\n…" if len(rows) > lines else "")


def _unfence(code: str) -> str:
    m = re.fullmatch(r"\s*```[\w+-]*[ \t]*\n(.*?)\n?```\s*", code, re.S)
    return (m.group(1) if m else code).strip("\n")


def _python_error(code: str, target: str) -> str:
    try:
        compile(code, target, "exec")
    except SyntaxError as e:
        return f"The code has a syntax error on line {e.lineno}: {e.msg}."
    return ""
