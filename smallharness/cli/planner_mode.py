"""Planner mode in the terminal: plan code changes, expand the steps, run them."""
from __future__ import annotations

from .. import ui
from ..planning import (
    Plan,
    PlanRunner,
    create_plan,
    draft_plan,
    edit_plan,
    expand,
    plan_text,
)
from .shell import Shell

HELP = [
    ("GOAL", "make a plan for this goal"),
    ("/show", "the current plan, with each step's details"),
    ("/plans, /load ID", "saved plans; open one"),
    ("/draft", "write a plan yourself (no model)"),
    ("/edit [ID]", "edit the plan and its details as text ($EDITOR, or typed)"),
    ("/expand [N,M|all] [times=K]", "one more pass of detail for those steps (all by default)"),
    ("/run [fresh|continuous] [parallel]", "carry out the plan (risky tools ask first)"),
    ("/tools", "the tools plan runs can use"),
]


class PlannerShell(Shell):
    name = "planner"
    about = "plan code changes from a goal; expand each step pass by pass; run the plan"
    hint = "type a goal to plan it"
    help = HELP

    def __init__(self, h, events, no_color: bool = False):
        super().__init__(h, events, no_color)
        self.plan: Plan | None = h.plans.latest()

    def banner(self) -> None:
        super().banner()
        if self.plan:
            print(ui.dim(f"  current plan {self.plan.id}: {self.plan.goal} (/show)"))

    # ------------------------------------------------------------ making
    def text(self, line: str):
        with self.busy("planning"):
            self.plan, warnings = create_plan(self.h, line)
        self.show()
        for w in warnings:
            print("  " + ui.warn(w))
        print(ui.dim("  /expand for more detail · /edit to change it · /run to carry it out"))

    def cmd_draft(self, _):
        self._edit(None)

    def cmd_edit(self, arg):
        plan = self.h.plans.load(arg) if arg else self._current()
        if plan:
            self._edit(plan)

    def _edit(self, plan: Plan | None) -> None:
        """The plan as text: a GOAL line, then a line per step with its details indented."""
        from ..notemode import write_text
        print(ui.dim("  GOAL: on the first line; then one step per line, N. [tool] what to do (after: N),"
                     " with its details on indented lines below it."
                     + (" Unchanged steps keep their status." if plan else "")))
        print(ui.dim("  tools: none, " + ", ".join(self.h.tools.tools)))
        initial = f"GOAL: {plan.goal}\n\n{plan_text(plan)}\n" if plan else "GOAL: \n\n"
        text = write_text(initial, "plan")
        if not text:
            print(ui.dim("  unchanged" if plan else "  cancelled"))
            return
        lines = text.rstrip().splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        goal = ""
        if lines and lines[0].upper().startswith("GOAL:"):
            goal, lines = lines[0][5:].strip(), lines[1:]
        if plan:
            warnings = edit_plan(self.h, plan, goal, "\n".join(lines))
        else:
            plan, warnings = draft_plan(self.h, goal, "\n".join(lines))
        self.plan = plan
        self.show()
        for w in warnings:
            print("  " + ui.warn(w))

    # ------------------------------------------------------------ showing
    def _current(self) -> Plan | None:
        if not self.plan:
            print(ui.dim("  no plan yet: type a goal, or /draft"))
            return None
        self.plan = self.h.plans.load(self.plan.id)      # pick up edits made elsewhere
        return self.plan

    def show(self) -> None:
        p = self.plan
        print(ui.accent(ui.bold(f"PLAN {p.id}")) + f"  {p.goal}")
        for t in p.tasks:
            depth = ui.dim(f"  ·{'·' * (t.depth - 1)} pass {t.depth}") if t.depth else ""
            print(t.line() + depth)
            for ln in t.detail.splitlines():
                print(ui.steel(f"      {ln}"))
            if t.result:
                print(ui.dim(f"      -> {t.result}"))

    def cmd_show(self, _):
        if self._current():
            self.show()

    def cmd_plans(self, _):
        for p in self.h.plans.list(20):
            done = sum(1 for t in p.tasks if t.status == "done")
            mark = ui.accent("◆") if self.plan and p.id == self.plan.id else " "
            print(f"  {mark} {ui.sky(p.id)}  {p.goal}" + ui.dim(f"  {done}/{len(p.tasks)} done"))

    def cmd_load(self, arg):
        self.plan = self.h.plans.load(arg)
        self.show()

    def cmd_tools(self, _):
        for t in self.h.tools:
            label = ui.risky("asks first") if t.needs_permission else ui.sky("runs freely")
            print(f"  {label}  {ui.bold(t.signature())}\n      {ui.dim(t.description)}")

    # ---------------------------------------------------------- expanding
    def cmd_expand(self, arg):
        plan = self._current()
        if not plan:
            return
        words = arg.replace(",", " ").split()
        times = next((int(w[6:]) for w in words if w.startswith("times=") and w[6:].isdigit()), 1)
        nums = [int(w) for w in words if w.isdigit()] or None
        for i in range(times):
            label = f"expanding (pass {i + 1} of {times})" if times > 1 else "expanding"
            with self.busy(label):
                expand(self.h, plan, nums, on_end=lambda t: setattr(self.ev.spinner, "label",
                                                                    f"{label}: step {t.num} done"))
        self.show()

    # ------------------------------------------------------------- running
    def cmd_run(self, arg):
        plan = self._current()
        if not plan:
            return
        words = arg.split()
        mode = next((w for w in words if w in ("fresh", "continuous")), self.h.cfg["plan"]["default_mode"])
        runner = PlanRunner(self.h, plan, mode, self.ev, parallel=("parallel" in words) or None)

        def start(t):
            print(ui.accent(f"▶ task {t.num}: {t.task}") + ui.dim(f"  [{t.tool}] · {mode} context"))
            self.ev.spinner.start("working")

        def end(t):
            self.ev.spinner.stop()
            self.ev.on_text_end()
            text = f"{t.status.upper()}: {t.result}"
            print("  " + (ui.ok(f"✓ {text}") if t.status == "done" else
                          ui.err(text) if t.status == "failed" else ui.pale(f"– {text}")))

        def cont(t):
            return ui.confirm(f"task {t.num} failed. Continue with the rest?", tty_only=False)

        try:
            runner.run(start, end, cont)
        finally:
            self.ev.spinner.stop()
        print(ui.box(plan.render(results=True).splitlines(), "Plan result", ui.deep))
