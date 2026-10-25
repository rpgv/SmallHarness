"""The planner: plans for code changes, made from a goal in a clean context, made more
detailed pass by pass, and carried out task by task.

- Expanding: each pass rewrites the details of the chosen steps in more depth (3 lines
  on the first pass, then 6, 12, up to 24), one clean-context call per step that sees
  the goal, the step and its current details. Steps are expanded in parallel on as
  many model instances as RAM allows.
- Plans and their details are plain text to edit (plans.plan_text).
- Running: mode "fresh" gives each task a new, empty context (the plan, the task's
  details and the results of the tasks it depends on); "continuous" carries one
  context across all tasks. Runs use tools, and nothing else: no search, no notes."""
from __future__ import annotations

import re

from . import prompts
from .agent import Agent, Events, ForwardingEvents, Harness
from .context import RagStore, Session
from .errors import Invalid
from .plans import Plan, PlanStore, Task, parse_hand_tasks, parse_plan, plan_text
from .steps import back_to_pending

__all__ = ["Plan", "PlanStore", "Task", "parse_plan", "plan_text", "create_plan", "draft_plan", "edit_plan",
           "expand", "PlanRunner"]

_RESULT = re.compile(r"^\s*(DONE|FAILED)\s*:\s*(.*)$", re.I | re.M)
MAX_DETAIL_LINES = 24


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
    plan = h.plans.add(Plan(goal, tasks[:max_tasks], mode=h.cfg["plan"]["default_mode"]))
    return plan, warnings


def draft_plan(h: Harness, goal: str, text: str) -> tuple[Plan, list[str]]:
    """A plan written by hand (with details, if any): no model call."""
    tasks, warnings = parse_hand_tasks(text, set(h.tools.tools))
    plan = h.plans.add(Plan(goal.strip() or "(no goal)", tasks, mode=h.cfg["plan"]["default_mode"]))
    return plan, warnings


def edit_plan(h: Harness, plan: Plan, goal: str, text: str) -> list[str]:
    """Replace a plan's goal, tasks and details. A task left as it was keeps its status,
    result and expansion depth; a changed or new one is pending again."""
    tasks, warnings = parse_hand_tasks(text, set(h.tools.tools))
    old = {(t.task, t.tool, tuple(t.after)): t for t in plan.tasks}
    for t in tasks:
        prev = old.get((t.task, t.tool, tuple(t.after)))
        if prev and prev.status != "running":
            t.status, t.result, t.depth = prev.status, prev.result, prev.depth
    plan.goal, plan.tasks = goal.strip() or plan.goal, tasks
    h.plans.save(plan)
    return warnings


def detail_lines(depth: int) -> int:
    """How long a step's details may be after its next pass: 3, 6, 12, then 24."""
    return min(3 * 2 ** depth, MAX_DETAIL_LINES)


def expand(h: Harness, plan: Plan, nums: list[int] | None = None, parallel: bool = True,
           on_end=None) -> list[Task]:
    """One expansion pass over the steps `nums` (all when None): each step's details are
    rewritten in more depth by a clean-context call that sees the goal, the step and
    its current details. Steps run in parallel on as many instances as RAM allows."""
    steps = [t for t in plan.tasks if nums is None or t.num in nums]
    missing = sorted(set(nums or []) - {t.num for t in steps})
    if missing:
        raise Invalid(f"no step {missing[0]}: the plan has {len(plan.tasks)}")

    def one(t: Task) -> Task:
        current = f"CURRENT DETAILS:\n{t.detail.strip()}\n" if t.detail.strip() else ""
        lines = detail_lines(t.depth)
        reply = h.generate_clean(prompts.EXPAND.format(num=t.num, goal=plan.goal, current=current, lines=lines,
                                                       step=f"[{t.tool}] {t.task}"), prompts.EXPAND_SYSTEM)
        text = _clean_detail(reply, lines)
        if text:
            t.detail, t.depth = text, t.depth + 1
        if on_end:
            on_end(t)
        return t
    done = h.pool.parallel(one, steps) if parallel else [one(t) for t in steps]
    h.plans.save(plan)
    return done


def _clean_detail(text: str, lines: int) -> str:
    """The reply as details: no preamble label, no tool calls, at most `lines` lines."""
    text = re.sub(r"^\s*(?:DETAILS?|STEP \d+|HOW TO DO IT)\s*:\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"<tool>.*?</tool>", "", text, flags=re.S)
    kept = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(kept[:lines])


class PlanRunner:
    """mode="fresh": each task runs in a new, empty context (plan, details, dependency
    results). mode="continuous": one context carries across all tasks; the context
    manager refreshes it as it fills. With `parallel` (fresh only), independent tasks
    run at the same time on as many instances as RAM allows."""

    def __init__(self, h: Harness, plan: Plan, mode: str = "fresh", events: Events | None = None,
                 parallel: bool | None = None):
        if mode not in ("fresh", "continuous"):
            raise ValueError("mode must be 'fresh' or 'continuous'")
        self.h, self.plan, self.mode = h, plan, mode
        self.events = events or h.events
        self.parallel = h.cfg["plan"]["parallel"] if parallel is None else parallel
        self.store = h.plans
        self._continuous_ctx = None
        plan.mode = mode

    def _context(self):
        """A context of its own, with no search results: runs see only what the plan gives."""
        if self.mode == "continuous":
            if self._continuous_ctx is None:
                self._continuous_ctx = self.h.new_context(Session(None), RagStore(None), retrieve=False)
            return self._continuous_ctx
        return self.h.new_context(Session(None), RagStore(None), retrieve=False)

    def _prompt(self, t: Task) -> str:
        deps = ""
        if t.after:
            lines = [f"- task {a}: {self.plan.get(a).result}" for a in t.after if self.plan.get(a)]
            deps = "RESULTS OF EARLIER TASKS:\n" + "\n".join(lines) + "\n\n"
        hint = f"Suggested tool: {t.tool}" if t.tool != "none" else "No tool needed."
        details = f"HOW TO DO IT:\n{t.detail.strip()}\n" if t.detail.strip() else ""
        return prompts.TASK.format(plan=self.plan.render(results=False), deps=deps, num=t.num,
                                   task=t.task, tool_hint=hint, details=details)

    def run_task(self, t: Task, events: Events | None = None) -> Task:
        t.status = "running"
        agent = Agent(self.h, self._context(), events or self.events)
        try:
            answer = agent.respond(self._prompt(t), auto_tools=True, stream=events is None)
        except Exception as e:
            t.status, t.result = "failed", f"{type(e).__name__}: {e}"
            return t
        found = _RESULT.findall(answer)
        if found:
            verdict, note = found[-1]
            t.status = "done" if verdict.upper() == "DONE" else "failed"
            t.result = note.strip()[:300]
        else:
            t.status = "done"
            t.result = (answer.strip().splitlines() or ["(no output)"])[-1][:300] + " (no DONE line)"
        return t

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


def _quiet(events: Events) -> Events:
    """Tool and permission events, but not streamed text (parallel runs interleave)."""
    return ForwardingEvents(events, drop=("on_text", "on_text_end", "on_refresh", "on_stats"))
