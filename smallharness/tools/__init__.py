"""Tool registry. A tool is one Python file dropped into a tools folder:

    NAME = "safe_read_file"          # prefix states the risk (see below)
    DESCRIPTION = "Read a text file with line numbers."
    PARAMS = {
        "path":  {"type": "str", "desc": "file path relative to the workspace"},
        "start": {"type": "int", "desc": "first line", "default": 1},
    }
    def run(args: dict, ctx: ToolContext) -> str: ...

Risk is part of the name, so it is visible wherever the tool shows up:
    safe_*   type 2 - runs without asking (read-only, or writes only harness state)
    risky_*  type 1 - always asks the user before every call
Files whose NAME has neither prefix are rejected.

Folders searched, later ones overriding earlier: smallharness/tools/builtin,
<workspace>/.smallharness/tools, then [tools].extra_dirs.
"""
from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterator

BUILTIN_DIR = Path(__file__).parent / "builtin"
RISK_PREFIXES = {"safe_": 2, "risky_": 1}
_TYPES: dict[str, Callable[[Any], Any]] = {
    "str": str, "int": int, "float": float,
    "bool": lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "y"),
}


@dataclass
class ToolContext:
    workspace: Path
    cfg: Any = None
    project_rag: Any = None
    session_rag: Any = None
    session: Any = None
    extra: dict = field(default_factory=dict)
    notes: Any = None              # notes.NoteStore

    def resolve(self, path: str) -> Path:
        """Resolve a path and refuse anything outside the workspace."""
        p = (self.workspace / path).resolve()
        if not p.is_relative_to(self.workspace):
            raise PermissionError(f"{path!r} is outside the workspace")
        return p

    def inside(self, p: Path) -> bool:
        """True if `p`, with symlinks followed, is inside the workspace."""
        try:
            return p.resolve().is_relative_to(self.workspace)
        except (OSError, RuntimeError):  # unreadable, or a symlink loop
            return False

    def glob(self, base: Path, pattern: str) -> Iterator[Path]:
        """Paths under `base` matching `pattern`, kept inside the workspace: a pattern
        that climbs out ("..") or is absolute is refused, and matches that are, or
        pass through, a symlink to somewhere outside are left out."""
        if PurePosixPath(pattern).is_absolute() or PureWindowsPath(pattern).is_absolute() \
                or ".." in re.split(r"[\\/]+", pattern):
            raise PermissionError(f"pattern {pattern!r} must stay inside the workspace "
                                  "(no '..' and no absolute paths)")
        return (p for p in base.glob(pattern) if self.inside(p))


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, dict]
    run: Callable[[dict, ToolContext], str]
    source: Path | None = None

    @property
    def risk_type(self) -> int:
        """1 = always needs permission, 2 = runs freely."""
        return next(t for p, t in RISK_PREFIXES.items() if self.name.startswith(p))

    @property
    def needs_permission(self) -> bool:
        return self.risk_type == 1

    def signature(self) -> str:
        args = ", ".join(k if "default" not in v else f"{k}?" for k, v in self.params.items())
        return f"{self.name}({args})"

    def usage(self) -> str:
        lines = [f"{self.signature()} - {self.description}"]
        for k, v in self.params.items():
            opt = f" (default {v['default']!r})" if "default" in v else " (required)"
            lines.append(f"    {k}: {v.get('type', 'str')}{opt} - {v.get('desc', '')}")
        return "\n".join(lines)

    def validate(self, args: dict) -> dict:
        out = {}
        unknown = set(args) - set(self.params)
        if unknown:
            raise ValueError(f"unknown argument(s) {sorted(unknown)}; expected {list(self.params)}")
        for k, spec in self.params.items():
            if k in args and args[k] is not None:
                try:
                    out[k] = _TYPES[spec.get("type", "str")](args[k])
                except (TypeError, ValueError):
                    raise ValueError(f"argument {k!r} must be {spec.get('type', 'str')}")
            elif "default" in spec:
                out[k] = spec["default"]
            else:
                raise ValueError(f"missing required argument {k!r}")
        return out


@dataclass
class ToolOutcome:
    name: str
    ok: bool
    text: str
    denied: bool = False


Permit = Callable[[Tool, dict], bool]


class ToolRegistry:
    def __init__(self, dirs: list[Path] | None = None):
        self.dirs = [BUILTIN_DIR] + list(dirs or [])
        self.tools: dict[str, Tool] = {}
        self.warnings: list[str] = []

    def load(self) -> "ToolRegistry":
        self.tools.clear()
        self.warnings.clear()
        for d in self.dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                try:
                    self.register(self._load_file(f))
                except Exception as e:  # a broken user tool must not break the harness
                    self.warnings.append(f"{f}: {e}")
        return self

    @staticmethod
    def _load_file(f: Path) -> Tool:
        spec = importlib.util.spec_from_file_location(f"smallharness_tool_{f.stem}", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for attr in ("NAME", "DESCRIPTION", "run"):
            if not hasattr(mod, attr):
                raise ValueError(f"missing {attr}")
        return Tool(mod.NAME, mod.DESCRIPTION.strip(), getattr(mod, "PARAMS", {}), mod.run, f)

    def register(self, tool: Tool) -> None:
        if not any(tool.name.startswith(p) for p in RISK_PREFIXES):
            raise ValueError(f"tool name {tool.name!r} must start with 'safe_' or 'risky_'")
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def __iter__(self):
        return iter(sorted(self.tools.values(), key=lambda t: (t.risk_type, t.name)))

    def param_orders(self) -> dict[str, list[str]]:
        """Tool name -> parameter names in order (for the tool-call parser)."""
        return {t.name: list(t.params) for t in self}

    def example(self, tool: Tool | None = None) -> str:
        """A well-formed call. Without a tool, the standard example uses a real tool,
        because small models copy concrete examples better than templates."""
        if tool is None:
            if "safe_read_file" in self.tools:
                return '<tool>{"name": "safe_read_file", "args": {"path": "README.md"}}</tool>'
            tool = next(iter(self), None)
            if tool is None:
                return ""
        values = {"str": "<{}>", "int": 1, "float": 1.0, "bool": True}
        args = {k: (lambda v: v.format(k) if isinstance(v, str) else v)(values.get(spec.get("type", "str"), "<{}>"))
                for k, spec in tool.params.items() if "default" not in spec}
        return "<tool>" + json.dumps({"name": tool.name, "args": args}) + "</tool>"

    def prompt_block(self) -> str:
        lines = [
            "TOOLS: to use one, reply with only the call, as JSON in tool tags, e.g.",
            self.example(),
            "Then wait for its TOOL RESULT. One tool per reply.",
        ]
        for t in self:
            args = ", ".join(k if "default" in v else f"{k} (required)" for k, v in t.params.items())
            lines.append(f"- {t.name}: {t.description.splitlines()[0]}" + (f" Args: {args}." if args else ""))
        return "\n".join(lines)

    def execute(self, name: str, args: dict, ctx: ToolContext, permit: Permit,
                max_chars: int = 3000) -> ToolOutcome:
        tool = self.get(name)
        if tool is None:
            close = [t.name for t in self if name.split("_", 1)[-1] in t.name]
            hint = f" Did you mean {close[0]}?" if close else ""
            return ToolOutcome(name, False, f"ERROR: no tool named {name!r}.{hint} "
                                            f"Available: {', '.join(self.tools)}")
        try:
            args = tool.validate(args)
        except ValueError as e:
            return ToolOutcome(name, False, f"ERROR: {e}\nUsage: {tool.usage()}")
        if tool.needs_permission and not permit(tool, args):
            return ToolOutcome(name, False, "DENIED: the user did not allow this call. "
                                            "Do not retry it; continue without it or ask the user.", denied=True)
        try:
            text = str(tool.run(args, ctx))
        except Exception as e:
            return ToolOutcome(name, False, f"ERROR: {type(e).__name__}: {e}")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…[truncated {len(text) - max_chars} chars]"
        return ToolOutcome(name, True, text)


def format_args(args: dict) -> str:
    return json.dumps(args, ensure_ascii=False)
