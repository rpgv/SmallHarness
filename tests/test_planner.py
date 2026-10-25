"""The planner: clean-context plans, expansion passes, editable details, runs without search."""
from unittest import mock

from smallharness.planning import (
    PlanRunner,
    create_plan,
    detail_lines,
    draft_plan,
    edit_plan,
    expand,
    plan_text,
)
from tests.helpers import WorkspaceTest


class Planner(WorkspaceTest):
    def test_plans_start_from_a_clean_context(self):
        h = self.harness()
        h.session.add("user", "an unrelated chat line about bananas")
        plan, _ = create_plan(h, "inventory the workspace")
        self.assertEqual([t.tool for t in plan.tasks], ["safe_list_files", "none"])
        self.assertNotIn("bananas", self.backend.prompts[-1])
        self.assertTrue(self.backend.prompts[-1].startswith("You write short, concrete plans"))

    def test_expansion_passes_go_deeper(self):
        h = self.harness()
        plan, _ = draft_plan(h, "add a flag", "[safe_read_file] Read the CLI\n[none] Add --verbose (after: 1)")
        self.assertEqual([detail_lines(d) for d in range(5)], [3, 6, 12, 24, 24])
        self.backend.script.extend(["Read cli.py.\nFind the parser.\nNote the options.\nToo long a line."])
        expand(h, plan, [1], parallel=False)
        t1, t2 = plan.tasks
        self.assertEqual((t1.depth, t1.detail.count("\n") + 1, t2.depth), (1, 3, 0))   # cut to 3 lines; step 2 untouched
        expand(h, plan, [1], parallel=False)
        prompt = self.backend.prompts[-1]
        self.assertIn("CURRENT DETAILS:\nRead cli.py.", prompt)
        self.assertIn("at most 6 lines", prompt)
        self.assertEqual(t1.depth, 2)
        self.assertEqual(h.plans.load(plan.id).tasks[0].depth, 2)                       # saved
        with self.assertRaises(ValueError):
            expand(h, plan, [9])

    def test_steps_expand_in_parallel(self):
        h = self.harness()
        plan, _ = draft_plan(h, "g", "a\nb\nc")
        with mock.patch.object(type(h.pool), "capacity", new_callable=mock.PropertyMock, return_value=3):
            expand(h, plan)
        self.assertEqual(len(h.pool.instances), 3)
        self.assertTrue(all(t.depth == 1 for t in plan.tasks))

    def test_details_are_plain_text_to_edit(self):
        h = self.harness()
        text = ("1. [safe_read_file] Read the CLI\n   Open cli/main.py.\n   1. find build_parser\n   2. note flags\n"
                "2. [none] Add --verbose (after: 1)")
        plan, _ = draft_plan(h, "add a flag", text)
        self.assertEqual(len(plan.tasks), 2)                                   # numbered detail lines stay details
        self.assertEqual(plan.tasks[0].detail, "Open cli/main.py.\n1. find build_parser\n2. note flags")
        self.assertEqual(plan_text(plan), text)                                # round trip
        plan.tasks[0].status, plan.tasks[0].depth = "done", 2
        edit_plan(h, plan, "", plan_text(plan).replace("note flags", "note every flag"))
        self.assertEqual((plan.tasks[0].status, plan.tasks[0].depth), ("done", 2))  # same task: kept
        self.assertIn("note every flag", plan.tasks[0].detail)

    def test_runs_use_details_and_no_search(self):
        h = self.harness(["DONE: read", "DONE: added"])
        h.project_rag.add_text("secret.md", "The launch code is tangerine.")
        plan, _ = draft_plan(h, "add a flag", "[none] Read the CLI\n   Look at build_parser.\n[none] Add --verbose")
        PlanRunner(h, plan, "fresh").run()
        self.assertIn("HOW TO DO IT:\nLook at build_parser.", self.backend.prompts[0])
        self.assertTrue(all("tangerine" not in p for p in self.backend.prompts))  # no RAG in runs
        self.assertEqual([t.status for t in plan.tasks], ["done", "done"])
        h2 = self.harness(["DONE: a", "DONE: b"])
        plan2, _ = draft_plan(h2, "g", "[none] First\n[none] Second")
        PlanRunner(h2, plan2, "continuous").run()
        self.assertIn("CURRENT TASK 1", self.backend.prompts[1])               # one context carried on

    def test_details_from_earlier_versions_are_read(self):
        h = self.harness()
        plan, _ = draft_plan(h, "g", "[none] Do it")
        spec = h.cfg.state.plans / f".{plan.id}" / "step-1.md"
        spec.parent.mkdir()
        spec.write_text("<!-- step 1: [none] Do it -->\nUse the old way.\n")
        plan.tasks[0].spec = str(spec.relative_to(h.cfg.state.plans))
        h.plans.save(plan)
        loaded = h.plans.load(plan.id)
        self.assertEqual((loaded.tasks[0].detail, loaded.tasks[0].depth), ("Use the old way.", 1))
