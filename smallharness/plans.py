"""Plans as data: a goal and numbered tasks, each with details that expansion passes
make deeper; their text form (what the model writes and people edit); and where saved
plans are kept. Creating, expanding and running plans is in planning.py.

The text form: one task per unindented line, `N. [tool] task (after: N)`, and the
task's details on the indented lines below it. An indented `check: [tool] what to
verify` line is a check: run after its task succeeds, and a failed check repeats the
task (as many times as the plan's persistence allows)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import HarnessError, Invalid, NotFound

_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:task\s*)?(\d+)[.):]\s*(?:\[([^\]]*)\])?\s*(.+?)\s*$", re.I)
_AFTER = re.compile(r"\(\s*after\s*:?\s*([\d,\s and]+)\)\s*$", re.I)
_CHECK = re.compile(r"^check\s*:\s*(?:\[([^\]]*)\])?\s*(.+?)\s*$", re.I)

PERSISTENCE_MAX = 10   # the top of the gauge: retry until the task succeeds or the run is stopped


@dataclass
class Check:
    """A sub-step that proves its task worked; if it fails, the task is done again."""
    task: str
    tool: str = "none"
    status: str = "pending"        # pending | done | failed
    result: str = ""

    def line(self) -> str:
        return f"check: [{self.tool}] {self.task}"


@dataclass
class Task:
    num: int
    task: str
    tool: str = "none"
    after: list[int] = field(default_factory=list)
    status: str = "pending"        # pending | running | done | failed | skipped
    result: str = ""
    detail: str = ""               # how to do it; deeper after each expansion pass
    depth: int = 0                 # expansion passes so far
    spec: str = ""                 # earlier versions: a hidden file with the details (read once)
    checks: list[Check] = field(default_factory=list)
    attempts: int = 0              # tries in the last run (more than 1: retried)

    def line(self) -> str:
        mark = {"pending": " ", "running": ">", "done": "x", "failed": "!", "skipped": "-"}[self.status]
        after = f" (after: {', '.join(map(str, self.after))})" if self.after else ""
        return f"[{mark}] {self.num}. [{self.tool}] {self.task}{after}"


@dataclass
class Plan:
    goal: str
    tasks: list[Task]
    id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))
    mode: str = "fresh"
    persistence: int = 0           # retries per task, 0 to PERSISTENCE_MAX (no limit)

    def render(self, results: bool = False) -> str:
        out = [f"GOAL: {self.goal}"]
        for t in self.tasks:
            out.append(t.line())
            out += [f"      {c.line()}" for c in t.checks] if results else []
            if results and t.result:
                out.append(f"      -> {t.result}")
        return "\n".join(out)

    def get(self, num: int) -> Task | None:
        return next((t for t in self.tasks if t.num == num), None)

    def ready(self) -> list[Task]:
        done = {t.num for t in self.tasks if t.status == "done"}
        return [t for t in self.tasks if t.status == "pending" and all(a in done for a in t.after)]

    def blocked(self) -> list[Task]:
        bad = {t.num for t in self.tasks if t.status in ("failed", "skipped")}
        return [t for t in self.tasks if t.status == "pending" and any(a in bad for a in t.after)]

    def mark(self, nums: list[int] | None, done: bool) -> None:
        """Tick tasks off by hand, or untick them so the next run does them again
        (`nums` None: every task)."""
        tasks = self.tasks if nums is None else [self.get(n) for n in nums]
        missing = [n for n, t in zip(nums or [], tasks) if t is None]
        if missing:
            raise NotFound(f"no task {missing[0]}")
        for t in tasks:
            if t.status == "running":
                raise Invalid(f"task {t.num} is running")
        for t in tasks:
            if done and t.status != "done":
                t.status, t.result = "done", "ticked off by hand"
            elif not done:
                t.status, t.result, t.attempts = "pending", "", 0
                for c in t.checks:
                    c.status, c.result = "pending", ""

    @property
    def finished(self) -> bool:
        return all(t.status != "pending" for t in self.tasks)

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        tasks = [Task(**dict(t, checks=[Check(**c) for c in t.get("checks", [])])) for t in data["tasks"]]
        return cls(**dict(data, tasks=tasks))


class PlanStore:
    """Saved plans: `<id>.json`, and a readable `<id>.md` beside it."""
    _ID = re.compile(r"[\w-]+")

    def __init__(self, directory: Path):
        self.dir = directory

    def _path(self, plan_id: str) -> Path:
        if not self._ID.fullmatch(plan_id or ""):
            raise Invalid(f"bad plan id {plan_id!r}")
        return self.dir / f"{plan_id}.json"

    def exists(self, plan_id: str) -> bool:
        try:
            return self._path(plan_id).is_file()
        except Invalid:
            return False

    def load(self, plan_id: str) -> Plan:
        path = self._path(plan_id)
        if not path.is_file():
            raise NotFound(f"no plan {plan_id}")
        plan = Plan.from_dict(json.loads(path.read_text()))
        for t in plan.tasks:              # details kept in a hidden file by earlier versions
            if t.spec and not t.detail and (self.dir / t.spec).is_file():
                t.detail = (self.dir / t.spec).read_text(encoding="utf-8").partition("\n")[2].strip()
                t.depth = max(t.depth, 1)
            t.spec = ""
        return plan

    def ids(self) -> list[str]:
        """Newest first (ids start with the time they were made)."""
        return sorted((p.stem for p in self.dir.glob("*.json")), reverse=True)

    def latest(self) -> Plan | None:
        return next(iter(self.list(1)), None)

    def list(self, limit: int = 50) -> list[Plan]:
        """The newest readable plans; a file edited into something unreadable is skipped."""
        out = []
        for pid in self.ids():
            if len(out) >= limit:
                break
            try:
                out.append(self.load(pid))
            except (ValueError, KeyError, TypeError, HarnessError):
                continue
        return out

    def save(self, plan: Plan) -> None:
        self._path(plan.id).write_text(json.dumps(asdict(plan), indent=2))
        (self.dir / f"{plan.id}.md").write_text(f"GOAL: {plan.goal}\n\n{plan_text(plan)}\n")

    def delete(self, plan_id: str) -> None:
        path = self._path(plan_id)
        if not path.is_file():
            raise NotFound(f"no plan {plan_id}")
        path.unlink()
        path.with_suffix(".md").unlink(missing_ok=True)

    def add(self, plan: Plan) -> Plan:
        """Save a new plan under an id no other plan has (two plans in one second)."""
        base, n = plan.id, 2
        while self.exists(plan.id):
            plan.id, n = f"{base}-{n}", n + 1
        self.save(plan)
        return plan



def _tool(name: str | None, known_tools: set[str], where: str, warnings: list[str]) -> str:
    tool = (name or "none").strip()
    if tool.lower() in ("", "none", "-", "no tool"):
        return "none"
    if tool not in known_tools:
        warnings.append(f"{where}: unknown tool {tool!r}, set to none")
        return "none"
    return tool


def parse_check(line: str, known_tools: set[str], where: str, warnings: list[str]) -> Check | None:
    """`check: [tool] what to verify`, or None for any other line."""
    m = _CHECK.match(line.strip())
    return Check(m.group(2), _tool(m.group(1), known_tools, where, warnings)) if m else None


def parse_plan(text: str, known_tools: set[str]) -> tuple[list[Task], list[str]]:
    tasks, warnings = [], []
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        num, body = int(m.group(1)), m.group(3)
        after: list[int] = []
        am = _AFTER.search(body)
        if am:
            after = [int(x) for x in re.findall(r"\d+", am.group(1))]
            body = body[:am.start()].strip()
        tool = _tool(m.group(2), known_tools, f"task {num}", warnings)
        tasks.append(Task(num, body, tool, after))
    # Renumber defensively: models repeat or skip numbers.
    nums = [t.num for t in tasks]
    if len(set(nums)) != len(nums):
        warnings.append("duplicate task numbers; renumbered in order")
        for i, t in enumerate(tasks, 1):
            t.num = i
    valid = {t.num for t in tasks}
    for t in tasks:
        bad = [a for a in t.after if a not in valid or a >= t.num]
        if bad:
            warnings.append(f"task {t.num}: dropped invalid dependencies {bad}")
            t.after = [a for a in t.after if a not in bad]
    return tasks, warnings


DETAIL_INDENT = "   "


def plan_text(plan: Plan, details: bool = True) -> str:
    """The plan as editable text: a line per task, its details and checks indented below it."""
    out = []
    for t in plan.tasks:
        out.append(f"{t.num}. [{t.tool}] {t.task}" + (f" (after: {', '.join(map(str, t.after))})" if t.after else ""))
        if details and t.detail.strip():
            out += [DETAIL_INDENT + ln if ln.strip() else "" for ln in t.detail.strip().splitlines()]
        out += [DETAIL_INDENT + c.line() for c in t.checks]
    return "\n".join(out)


def parse_hand_tasks(text: str, known_tools: set[str]) -> tuple[list[Task], list[str]]:
    """The text form, as people write and edit it: an unindented line is a task (one
    without a number gets the next one); indented lines below it are its details, and
    its checks (`check: [tool] what to verify`)."""
    lines, details, checks, n, warnings = [], {}, {}, 0, []
    for raw in text.splitlines():
        if not raw.strip():
            if lines:
                details.setdefault(len(lines) - 1, []).append("")
            continue
        if raw[:1] in (" ", "\t") and lines:
            check = parse_check(raw, known_tools, f"a check of task {len(lines)}", warnings)
            if check:
                checks.setdefault(len(lines) - 1, []).append(check)
            else:
                details.setdefault(len(lines) - 1, []).append(raw.strip())
            continue
        m = _LINE.match(raw)
        n = int(m.group(1)) if m else n + 1
        lines.append(raw if m else f"{n}. {raw.strip().lstrip('-*').strip()}")
    tasks, task_warnings = parse_plan("\n".join(lines), known_tools)
    if not tasks:
        raise Invalid("no tasks: write one per line, like `1. [safe_read_file] Read the README (after: …)`")
    for i, t in enumerate(tasks):
        t.detail = "\n".join(details.get(i, [])).strip()
        t.checks = checks.get(i, [])
    return tasks, task_warnings + warnings
