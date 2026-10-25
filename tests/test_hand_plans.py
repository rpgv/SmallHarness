"""Plans drafted and edited by hand, in the terminal (the web routes are in test_web)."""
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from smallharness import cli
from smallharness.planning import (
    PlanRunner,
    create_plan,
    draft_plan,
    edit_plan,
    plan_text,
)
from tests.helpers import WorkspaceTest


class Base(WorkspaceTest):
    pass



class HandPlans(Base):
    def test_draft_and_edit(self):
        h = self.harness(["DONE: listed", "DONE: summed"])
        plan, warnings = draft_plan(h, "tidy up", "[safe_list_files] List files\n- [none] Sum up (after: 1)\n[nope] x")
        self.assertEqual(self.backend.prompts, [])                            # no model
        self.assertEqual([(t.num, t.tool, t.after) for t in plan.tasks],
                         [(1, "safe_list_files", []), (2, "none", [1]), (3, "none", [])])
        self.assertIn("unknown tool", warnings[0])
        PlanRunner(h, plan, "fresh").run()
        self.assertEqual(plan.tasks[0].status, "done")
        text = plan_text(plan).replace("3. [none] x", "3. [none] Write it down")
        edit_plan(h, plan, "", text)
        self.assertEqual(plan.goal, "tidy up")
        self.assertEqual([t.status for t in plan.tasks], ["done", "done", "pending"])   # unchanged tasks keep theirs
        self.assertEqual(plan.tasks[0].result, "listed")
        with self.assertRaises(ValueError):
            edit_plan(h, plan, "", "\n\n")

    def test_terminal_draft_and_edit(self):
        h = self.harness()
        shell = cli.PlannerShell(h, cli.TerminalEvents(), True)
        typed = iter(["GOAL: tidy the docs", "", "[safe_list_files] List the docs", "   only the docs/ folder",
                      "[none] Summarize (after: 1)", "."])
        with mock.patch.dict("os.environ", {"VISUAL": "", "EDITOR": ""}), \
                mock.patch("builtins.input", lambda _="": next(typed)), redirect_stdout(io.StringIO()) as out:
            shell.command("/draft")
        self.assertEqual(self.backend.prompts, [], out.getvalue())                        # no model
        self.assertEqual((shell.plan.goal, len(shell.plan.tasks)), ("tidy the docs", 2))
        self.assertEqual(shell.plan.tasks[0].detail, "only the docs/ folder")              # indented: details
        shell.plan.tasks[0].status = "done"
        h.plans.save(shell.plan)
        typed = iter(["1. [safe_list_files] List the docs", "   only the docs/ folder", "2. [none] Summarize (after: 1)",
                      "3. [none] Stop", "."])
        with mock.patch.dict("os.environ", {"VISUAL": "", "EDITOR": ""}), \
                mock.patch("builtins.input", lambda _="": next(typed)), redirect_stdout(io.StringIO()):
            shell.command("/edit")
        self.assertEqual(shell.plan.goal, "tidy the docs")                                 # kept: no GOAL line
        self.assertEqual([t.status for t in shell.plan.tasks], ["done", "pending", "pending"])
        with redirect_stdout(io.StringIO()):
            shell.text("edit the README")                                                 # plain text: a goal
        self.assertEqual(shell.plan.goal, "edit the README")

    def test_model_plans_are_editable_too(self):
        h = self.harness()
        plan, _ = create_plan(h, "inventory")
        self.assertIn("1. [safe_list_files]", plan_text(plan))
        edit_plan(h, plan, "inventory, then stop", plan_text(plan) + "\n3. [none] Stop")
        self.assertEqual(len(plan.tasks), 3)


class Storage(Base):
    def test_plan_store(self):
        from smallharness.errors import Invalid, NotFound
        from smallharness.plans import Plan, PlanStore, Task
        store = PlanStore(self.ws)
        a = store.add(Plan("one", [Task(1, "x")], id="20260101-000000"))
        b = store.add(Plan("two", [Task(1, "y")], id="20260101-000000"))      # same second
        self.assertEqual((a.id, b.id), ("20260101-000000", "20260101-000000-2"))
        self.assertEqual(store.latest().goal, "two")
        self.assertEqual(store.load(a.id).tasks[0].task, "x")
        with self.assertRaises(Invalid):
            store.load("../etc/passwd")
        with self.assertRaises(NotFound):
            store.load("nope")
        (self.ws / "20991231-000000.json").write_text("{broken")
        self.assertEqual([p.goal for p in store.list()], ["two", "one"])      # the broken one is skipped
        self.assertEqual(store.latest().goal, "two")
        self.assertTrue((self.ws / f"{a.id}.md").is_file())                    # readable copy


if __name__ == "__main__":
    unittest.main()
