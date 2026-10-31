"""The planner: plans for code changes, made from a goal in a clean context, made more
detailed pass by pass, and carried out task by task.

- Expanding: each pass rewrites the details of the chosen steps in more depth (3 lines
  on the first pass, then 6, 12, up to 24), one clean-context call per step that sees
  the goal, the step and its current details. Steps are expanded in parallel on as
  many model instances as RAM allows.
- Plans and their details are plain text to edit (plans.plan_text).
- Running: mode "fresh" gives each task a new, empty context (the plan, the task's
  details and the results of the tasks it depends on); "continuous" carries one
  context across all tasks. Runs use tools, and nothing else: no search, no notes.
- Checks: after a task succeeds, each of its checks runs in a clean context; one that
  fails counts as the task failing.
- Persistence: how many times a failed task is tried again (PERSISTENCE_MAX: until it
  succeeds or the run is stopped). Every retry starts from a clean context holding the
  task and the previous attempt's failure, nothing more. A tool the user refuses ends
  the retries."""
from __future__ import annotations

import re

from . import prompts
from .agent import Agent, Events, ForwardingEvents, Harness
from .context import RagStore, Session
from .errors import Invalid
from .plans import PERSISTENCE_MAX, Check, Plan, PlanStore, Task, parse_check, parse_hand_tasks, parse_plan, plan_text
from .steps import back_to_pending
from .tools import ToolOutcome

__all__ = ["PERSISTENCE_MAX", "Check", "Plan", "PlanStore", "Task", "parse_plan", "plan_text", "create_plan", "draft_plan", "edit_plan",
           "expand", "PlanRunner"]

_RESULT = re.compile(r"^\s*(DONE|FAILED)\s*:\s*(.*)$", re.I | re.M)
MAX_DETAIL_LINES = 24
MAX_CHECKS = 3


def create_plan(h: Harness, goal: str, max_tasks: int = 8) -> tuple[Plan, list[str]]:
    """A plan from the goal alone, in a clean context."""
    tool_list = "\n".join(f"- {t.name}: {t.description.splitlines()[0]}" for t in h.tools)
    ask = prompts.PLAN.format(max_tasks=max_tasks, tools=tool_list, goal=goal)
    text = h.generate_clean(ask, prompts.PLAN_SYSTEM)
    tasks, warnings = parse_plan(text, set(h.tools.tools))
    if not tasks:  # one stricter retry
        text = h.generate_clean("That did not follow PLAN FORMAT. Reply with only the numbered task lines.",
                                prompts.PLAN_SYSTEM, [("user", ask), ("assistant", text.strip())])
        tasks, warnings = parse_plan(text, set(h.tools.tools))
    if not tasks:
        raise Invalid("the model did not produce a plan in the required format:\n" + text[:500])
    plan = h.plans.add(Plan(goal, tasks[:max_tasks], mode=h.cfg["plan"]["default_mode"],
                            persistence=default_persistence(h)))
    return plan, warnings


def default_persistence(h: Harness) -> int:
    return clamp_persistence(h.cfg["plan"].get("persistence", 0))


def clamp_persistence(value) -> int:
    try:
        return max(0, min(int(value), PERSISTENCE_MAX))
    except (TypeError, ValueError):
        raise Invalid(f"persistence is a number from 0 to {PERSISTENCE_MAX}") from None


def draft_plan(h: Harness, goal: str, text: str) -> tuple[Plan, list[str]]:
    """A plan written by hand (with details, if any): no model call."""
    tasks, warnings = parse_hand_tasks(text, set(h.tools.tools))
    plan = h.plans.add(Plan(goal.strip() or "(no goal)", tasks, mode=h.cfg["plan"]["default_mode"],
                            persistence=default_persistence(h)))
    return plan, warnings


def edit_plan(h: Harness, plan: Plan, goal: str, text: str) -> list[str]:
    """Replace a plan's goal, tasks and details. A task left as it was keeps its status,
    result and expansion depth; a changed or new one is pending again."""
    tasks, warnings = parse_hand_tasks(text, set(h.tools.tools))
    old = {(t.task, t.tool, tuple(t.after)): t for t in plan.tasks}
    for t in tasks:
        prev = old.get((t.task, t.tool, tuple(t.after)))
        if prev and prev.status != "running":
            t.status, t.result, t.depth, t.attempts = prev.status, prev.result, prev.depth, prev.attempts
            kept = {(c.task, c.tool): c for c in prev.checks}
            t.checks = [kept.get((c.task, c.tool), c) for c in t.checks]
    plan.goal, plan.tasks = goal.strip() or plan.goal, tasks
    h.plans.save(plan)
    return warnings


def detail_lines(depth: int) -> int:
    """How long a step's details may be after its next pass: 3, 6, 12, then 24."""
    return min(3 * 2 ** depth, MAX_DETAIL_LINES)


def expand(h: Harness, plan: Plan, nums: list[int] | None = None, parallel: bool = True,
           on_end=None, checks: bool = False) -> list[Task]:
    """One expansion pass over the steps `nums` (all when None): each step's details are
    rewritten in more depth by a clean-context call that sees the goal, the step and
    its current details. Steps run in parallel on as many instances as RAM allows.
    With `checks`, the model may also propose checks for each step (up to MAX_CHECKS)."""
    steps = [t for t in plan.tasks if nums is None or t.num in nums]
    missing = sorted(set(nums or []) - {t.num for t in steps})
    if missing:
        raise Invalid(f"no step {missing[0]}: the plan has {len(plan.tasks)}")

    def one(t: Task) -> Task:
        current = f"CURRENT DETAILS:\n{t.detail.strip()}\n" if t.detail.strip() else ""
        lines = detail_lines(t.depth)
        room = MAX_CHECKS - len(t.checks)
        ask_checks = prompts.EXPAND_CHECKS.format(n=room, tools=", ".join(h.tools.tools)) if checks and room > 0 else ""
        reply = h.generate_clean(prompts.EXPAND.format(num=t.num, goal=plan.goal, current=current, lines=lines,
                                                       step=f"[{t.tool}] {t.task}", checks=ask_checks),
                                 prompts.EXPAND_SYSTEM)
        if ask_checks:
            have = {c.task for c in t.checks}
            for c in _proposed_checks(reply, set(h.tools.tools))[:room]:
                if c.task not in have:
                    t.checks.append(c)
                    have.add(c.task)
        text = _clean_detail(reply, lines)
        if text:
            t.detail, t.depth = text, t.depth + 1
        if on_end:
            on_end(t)
        return t
    done = h.pool.parallel(one, steps) if parallel else [one(t) for t in steps]
    h.plans.save(plan)
    return done


def _proposed_checks(reply: str, known_tools: set[str]) -> list[Check]:
    found = (parse_check(ln.strip().lstrip("-*").strip(), known_tools, "a proposed check", [])
             for ln in reply.splitlines())
    return [c for c in found if c]


def _clean_detail(text: str, lines: int) -> str:
    """The reply as details: no preamble label, no tool calls or checks, at most `lines` lines."""
    text = re.sub(r"^\s*(?:DETAILS?|STEP \d+|HOW TO DO IT)\s*:\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"<tool>.*?</tool>", "", text, flags=re.S)
    kept = [ln.rstrip() for ln in text.splitlines()
            if ln.strip() and not re.match(r"^[-*\s]*check\s*:", ln, re.I)]
    return "\n".join(kept[:lines])


class PlanRunner:
    """mode="fresh": each task runs in a new, empty context (plan, details, dependency
    results). mode="continuous": one context carries across all tasks; the context
    manager refreshes it as it fills. With `parallel` (fresh only), independent tasks
    run at the same time on as many instances as RAM allows. `persistence` (default:
    the plan's) is how many times a failed task is tried again."""

    def __init__(self, h: Harness, plan: Plan, mode: str = "fresh", events: Events | None = None,
                 parallel: bool | None = None, persistence: int | None = None):
        if mode not in ("fresh", "continuous"):
            raise ValueError("mode must be 'fresh' or 'continuous'")
        self.h, self.plan, self.mode = h, plan, mode
        self.events = events or h.events
        self.parallel = h.cfg["plan"]["parallel"] if parallel is None else parallel
        self.store = h.plans
        self._continuous_ctx = None
        plan.mode = mode
        if persistence is not None:
            plan.persistence = clamp_persistence(persistence)

    def _context(self, fresh: bool = False):
        """A context of its own, with no search results: runs see only what the plan gives."""
        if self.mode == "continuous" and not fresh:
            if self._continuous_ctx is None:
                self._continuous_ctx = self.h.new_context(Session(None), RagStore(None), retrieve=False)
            return self._continuous_ctx
        return self.h.new_context(Session(None), RagStore(None), retrieve=False)

    def _prompt(self, t: Task, feedback: str = "") -> str:
        deps = ""
        if t.after:
            lines = [f"- task {a}: {self.plan.get(a).result}" for a in t.after if self.plan.get(a)]
            deps = "RESULTS OF EARLIER TASKS:\n" + "\n".join(lines) + "\n\n"
        details = f"HOW TO DO IT:\n{t.detail.strip()}\n" if t.detail.strip() else ""
        checks = prompts.TASK_CHECKS.format(checks="\n".join(f"- {c.task}" for c in t.checks)) if t.checks else ""
        previous = prompts.TASK_PREVIOUS.format(feedback=feedback) if feedback else ""
        return prompts.TASK.format(plan=self.plan.render(results=False), deps=deps, num=t.num, task=t.task,
                                   tool_hint=_hint(t.tool), details=details, checks=checks, previous=previous)

    @property
    def retries(self) -> float:
        p = clamp_persistence(self.plan.persistence)
        return float("inf") if p >= PERSISTENCE_MAX else p

    def run_task(self, t: Task, events: Events | None = None) -> Task:
        """The task, then its checks; while either fails and persistence allows, again,
        each time from a clean context that holds only the task and what went wrong."""
        watch = _Watch(events or self.events)
        feedback, t.attempts = "", 0
        for c in t.checks:
            c.status, c.result = "pending", ""
        while True:
            t.attempts += 1
            t.status = "running"
            self._attempt(t, watch, events is None, feedback, fresh=t.attempts > 1)
            if t.status == "done" and not watch.denied:
                failed = self._run_checks(t, watch, events is None)
                if failed is None:
                    return t
                t.status, t.result = "failed", f"check failed: {failed.task}: {failed.result}"[:300]
            if watch.denied:
                t.status = "failed"
                t.result = f"a tool was refused ({watch.denied}); not tried again"
                return t
            if t.attempts > self.retries:
                return t
            feedback = t.result
            watch.on_notice(f"task {t.num} failed ({t.result}); attempt {t.attempts + 1}"
                            + ("" if self.retries == float("inf") else f" of {int(self.retries) + 1}")
                            + ", from a clean context")

    def _attempt(self, t: Task, events: Events, stream: bool, feedback: str, fresh: bool) -> None:
        agent = Agent(self.h, self._context(fresh), events)
        agent.tool_ctx.extra["origin"] = f"plan:{self.plan.id}"
        try:
            answer = agent.respond(self._prompt(t, feedback), auto_tools=True, stream=stream)
        except Exception as e:
            t.status, t.result = "failed", f"{type(e).__name__}: {e}"
            return
        t.status, t.result = _verdict(answer)

    def _run_checks(self, t: Task, events: Events, stream: bool) -> Check | None:
        """Each check in a clean context; the first that fails, or None."""
        for c in t.checks:
            agent = Agent(self.h, self._context(fresh=True), events)
            agent.tool_ctx.extra["origin"] = f"plan:{self.plan.id}"
            prompt = prompts.CHECK.format(num=t.num, goal=self.plan.goal, task=t.task, result=t.result,
                                          check=c.task, tool_hint=_hint(c.tool))
            try:
                c.status, c.result = _verdict(agent.respond(prompt, auto_tools=True, stream=stream))
            except Exception as e:
                c.status, c.result = "failed", f"{type(e).__name__}: {e}"
            if c.status == "failed" or events.denied:
                return c
        return None

    def run(self, on_start=None, on_end=None, should_continue=None) -> Plan:
        """Run until every task is done, failed, or blocked. `should_continue(task)`
        is asked after a failure; returning False stops the run. Stopped part-way
        (Ctrl-C, or Stop in the web interface), the running task is pending again."""
        try:
            self._run(on_start, on_end, should_continue)
        except KeyboardInterrupt:
            back_to_pending(self.plan.tasks)
            self.store.save(self.plan)
            raise
        return self.plan

    def _run(self, on_start, on_end, should_continue) -> None:
        while not self.plan.finished:
            for t in self.plan.blocked():
                t.status, t.result = "skipped", "a dependency failed"
            ready = self.plan.ready()
            if not ready:
                break
            batch = ready if (self.parallel and self.mode == "fresh") else ready[:1]
            for t in batch:
                if on_start:
                    on_start(t)
            if len(batch) > 1:
                quiet = _quiet(self.events)
                self.h.pool.parallel(lambda task: self.run_task(task, quiet), batch)
            else:
                self.run_task(batch[0])
            self.store.save(self.plan)
            for t in batch:
                if on_end:
                    on_end(t)
            failed = next((t for t in batch if t.status == "failed"), None)
            if failed and should_continue and not should_continue(failed):
                break


def _hint(tool: str) -> str:
    return f"Suggested tool: {tool}" if tool != "none" else "No tool needed."


def _verdict(answer: str) -> tuple[str, str]:
    """(status, result) from a reply's last DONE: or FAILED: line."""
    found = _RESULT.findall(answer)
    if found:
        verdict, note = found[-1]
        return ("done" if verdict.upper() == "DONE" else "failed"), note.strip()[:300]
    return "done", (answer.strip().splitlines() or ["(no output)"])[-1][:300] + " (no DONE line)"


class _Watch(ForwardingEvents):
    """Passes events on, noting a tool call the user refused: retrying would only ask again."""

    def __init__(self, inner: Events):
        super().__init__(inner)
        self.denied = ""

    def on_tool_result(self, outcome: ToolOutcome) -> None:
        if outcome.denied:
            self.denied = outcome.name
        self.inner.on_tool_result(outcome)


def _quiet(events: Events) -> Events:
    """Tool and permission events, but not streamed text (parallel runs interleave)."""
    return ForwardingEvents(events, drop=("on_text", "on_text_end", "on_refresh", "on_stats"))
