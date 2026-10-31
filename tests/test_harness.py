import json
import tempfile
import unittest
from pathlib import Path

from smallharness.agent import Events
from smallharness.context import RagStore
from smallharness.context.rag import chunk_text
from smallharness.planning import PlanRunner, create_plan, parse_plan
from smallharness.resources import capacity, estimate_kv_cache_mb
from smallharness.toolcall import ToolCallError, parse_tool_call
from smallharness.tools import ToolRegistry
from tests.helpers import WorkspaceTest


class Recorder(Events):
    def __init__(self, allow=False):
        self.allow = allow
        self.asked, self.calls, self.refreshes, self.notices = [], [], [], []

    def permit(self, tool, args):
        self.asked.append(tool.name)
        return self.allow

    def on_tool_call(self, name, args):
        self.calls.append((name, args))

    def on_refresh(self, moved, before):
        self.refreshes.append(moved)

    def on_notice(self, msg):
        self.notices.append(msg)


class Base(WorkspaceTest):
    def setUp(self):
        super().setUp()
        (self.ws / "notes.txt").write_text("alpha line\nThe secret word is pineapple.\n")

    def harness(self, script=None, events=None, **over):
        return super().harness(script, events or Recorder(), **over)



class ToolCallParsing(unittest.TestCase):
    def test_canonical(self):
        c = parse_tool_call('<tool>{"name": "safe_read_file", "args": {"path": "a.py"}}</tool>')
        self.assertEqual((c.name, c.args), ("safe_read_file", {"path": "a.py"}))

    def test_lenient_forms(self):
        self.assertEqual(parse_tool_call("<tool>{'name': 'x', 'args': {'a': 1,},}").args, {"a": 1})
        self.assertEqual(parse_tool_call('<tool>safe_list_files {"pattern": "*.py"}</tool>').name, "safe_list_files")
        self.assertEqual(parse_tool_call('<tool>{"name": "x", "path": "p"}</tool>').args, {"path": "p"})
        self.assertEqual(parse_tool_call('```json\n{"name": "x", "args": {}}\n```').name, "x")
        self.assertEqual(parse_tool_call('<tool>{"name": "x", "args": {"a": "b"}').args, {"a": "b"})

    def test_plain_text_and_errors(self):
        self.assertIsNone(parse_tool_call("Just an answer."))
        with self.assertRaises(ToolCallError):
            parse_tool_call("<tool>{not json at all</tool>")


KNOWN = {"safe_read_file": ["path", "start", "lines"], "risky_run_command": ["command", "timeout"]}


class ToolCallDrift(unittest.TestCase):
    """Forms small models actually produce (regression for 'could not read the tool call')."""

    def call(self, text):
        c = parse_tool_call(text, KNOWN)
        return c and (c.name, c.args)

    def test_python_call_syntax(self):
        want = ("safe_read_file", {"path": "notes.md"})
        self.assertEqual(self.call('<tool>safe_read_file(path="notes.md")</tool>'), want)
        self.assertEqual(self.call('<tool>safe_read_file("notes.md")</tool>'), want)
        self.assertEqual(self.call('<tool>print(default_api.safe_read_file(path="notes.md"))</tool>'), want)
        self.assertEqual(self.call('```tool_code\nsafe_read_file(path="notes.md")\n```'), want)
        self.assertEqual(self.call('safe_read_file(path="notes.md")'), want)
        self.assertEqual(self.call('<tool_call>{"name": "safe_read_file", "args": {"path": "notes.md"}}</tool_call>'), want)

    def test_fence_and_words_inside_tag(self):
        want = ("safe_read_file", {"path": "notes.md"})
        self.assertEqual(self.call('<tool>\n```json\n{"name": "safe_read_file", "args": {"path": "notes.md"}}\n```\n</tool>'), want)
        self.assertEqual(self.call('<tool>Calling: {"name": "safe_read_file", "args": {"path": "notes.md"}}</tool>'), want)

    def test_json_words_in_call_syntax(self):
        self.assertEqual(parse_tool_call('<tool>safe_cag_create(name="a", content="b", pin=true)</tool>').args,
                         {"name": "a", "content": "b", "pin": True})

    def test_mentions_are_not_calls(self):
        echo = ('FAILED: ERROR: could not read the tool call inside <tool>…</tool>. Reply with exactly: '
                '<tool>{"name": "TOOL_NAME", "args": {...}}</tool>')
        self.assertIsNone(self.call("I can answer without a <tool> here."))
        with self.assertRaises(ToolCallError):  # JSON-looking body is still treated as an attempt
            parse_tool_call(echo, KNOWN)
        self.assertIsNone(self.call("FAILED: could not read the call inside <tool>…</tool>."))
        self.assertIsNone(self.call("```python\nprint(len(x))\n```"))

    def test_broken_call_names_the_tool(self):
        with self.assertRaises(ToolCallError) as cm:
            parse_tool_call("<tool>safe_read_file path=notes.md</tool>", KNOWN)
        self.assertEqual(cm.exception.name, "safe_read_file")


class RealModelOutputs(unittest.TestCase):
    """Exact gemma-4-E2B-it outputs from user test sessions (2026-10-11)."""

    def setUp(self):
        self.known = ToolRegistry().load().param_orders()
        # Recorded while safe_cag_create existed; the outputs stay verbatim.
        self.known["safe_cag_create"] = ["name", "content", "pin", "append"]

    def test_missing_closing_brace(self):
        c = parse_tool_call('<tool>{"name": "safe_search_files", "args": {"query": "FastAPI", "glob": "*" , "path": "."}</tool>',
                            self.known)
        self.assertEqual(c.args, {"query": "FastAPI", "glob": "*", "path": "."})

    def test_code_with_unescaped_quotes(self):
        code_json = ('"from fastapi import FastAPI\\n\\napp = FastAPI()\\n\\n@app.get(\\"/cpu\\")\\n'
                     'def get_cpu_usage():\\n    return {"cpu_usage": "N/A"} \\n\\nif __name__ == \\"__main__\\":\\n'
                     '    uvicorn.run(app, host=\\"0.0.0.0\\", port=8000)"')
        want = ('from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get("/cpu")\ndef get_cpu_usage():\n'
                '    return {"cpu_usage": "N/A"} \n\nif __name__ == "__main__":\n    uvicorn.run(app, host="0.0.0.0", port=8000)')
        forms = [
            '<tool>safe_cag_create{"name": "fastapi_app.py", "content": ' + code_json + '}</tool>\nDONE: Created',
            '<tool>{"name": "safe_cag_create", "args": {"name": "fastapi_app.py", "content": ' + code_json + '}</tool>',
            '<tool>{"name": "risky_write_file", "path": "app.py", "content": ' + code_json + '}</tool>',
        ]
        for text in forms:
            c = parse_tool_call(text, self.known)
            self.assertEqual(c.args["content"], want, text[:40])
        self.assertEqual(parse_tool_call(forms[0], self.known).args["name"], "fastapi_app.py")
        self.assertEqual(parse_tool_call(forms[2], self.known).args["path"], "app.py")


class Registry(unittest.TestCase):
    def test_builtin_risk_types(self):
        reg = ToolRegistry().load()
        self.assertFalse(reg.warnings)
        self.assertTrue(reg.get("risky_run_command").needs_permission)
        self.assertFalse(reg.get("safe_read_file").needs_permission)

    def test_drop_in_tool_and_bad_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "safe_echo.py").write_text(
                'NAME = "safe_echo"\nDESCRIPTION = "echo"\nPARAMS = {"x": {"type": "int", "desc": "n"}}\n'
                "def run(args, ctx):\n    return str(args['x'] * 2)\n")
            Path(d, "bad.py").write_text('NAME = "echo2"\nDESCRIPTION = "d"\ndef run(a, c): return ""\n')
            reg = ToolRegistry([Path(d)]).load()
            self.assertIn("safe_echo", reg.tools)
            self.assertNotIn("echo2", reg.tools)
            self.assertTrue(any("must start with" in w for w in reg.warnings))
            from smallharness.tools import ToolContext
            out = reg.execute("safe_echo", {"x": "21"}, ToolContext(Path(d)), lambda t, a: False)
            self.assertEqual(out.text, "42")


class AgentLoop(Base):
    def test_safe_tool_runs_without_asking(self):
        ev = Recorder()
        h = self.harness([
            '<tool>{"name": "safe_read_file", "args": {"path": "notes.txt"}}</tool>',
            "The secret word is pineapple.",
        ], ev)
        answer = h.agent().respond("what is the secret word?", stream=False)
        self.assertIn("pineapple", answer)
        self.assertEqual(ev.asked, [])
        self.assertIn("pineapple", self.backend.prompts[1])  # tool result was in the next prompt

    def test_risky_tool_asks_and_respects_denial(self):
        ev = Recorder(allow=False)
        h = self.harness(['<tool>{"name": "risky_run_command", "args": {"command": "touch x"}}</tool>',
                          "ok, not running it"], ev)
        h.agent().respond("make a file", stream=False)
        self.assertEqual(ev.asked, ["risky_run_command"])
        self.assertFalse((self.ws / "x").exists())
        self.assertIn("DENIED", self.backend.prompts[1])

    def test_risky_tool_runs_when_allowed(self):
        h = self.harness(['<tool>{"name": "risky_run_command", "args": {"command": "touch x"}}</tool>', "done"],
                         Recorder(allow=True))
        h.agent().respond("make a file", stream=False)
        self.assertTrue((self.ws / "x").exists())

    def test_bad_args_get_usage_hint(self):
        h = self.harness(['<tool>{"name": "safe_read_file", "args": {}}</tool>', "sorry"])
        h.agent().respond("read", stream=False)
        self.assertIn("missing required argument 'path'", self.backend.prompts[1])

    def test_step_limit(self):
        call = '<tool>{"name": "safe_list_files", "args": {}}</tool>'
        h = self.harness([call] * 10, tools={"max_steps": 2})
        h.agent().respond("loop", stream=False)
        self.assertEqual(len(self.backend.prompts), 3)
        self.assertIn("Tool limit reached", self.backend.prompts[-1])
        self.assertNotIn("TOOLS:", self.backend.prompts[-1])

    def test_python_style_call_runs(self):
        h = self.harness(['<tool>safe_read_file(path="notes.txt")</tool>', "pineapple"])
        self.assertEqual(h.agent().respond("secret?", stream=False), "pineapple")
        self.assertIn("The secret word is pineapple", self.backend.prompts[1])

    def test_unreadable_calls_do_not_loop(self):
        echo = 'ERROR: could not read <tool>{"name": "TOOL_NAME", "args": {...}}</tool>'
        h = self.harness(["<tool>safe_read_file path=notes.txt</tool>", echo, "plain answer"])
        out = h.agent().respond("secret?", stream=False)
        self.assertEqual(len(self.backend.prompts), 3)  # 2 failures, then one answer without tools
        self.assertEqual(out, "plain answer")
        self.assertNotIn("TOOLS:", self.backend.prompts[-1])
        help_text = self.backend.prompts[1]
        self.assertIn('"name": "safe_read_file"', help_text)  # example for the tool it wanted
        self.assertNotIn("TOOL_NAME", help_text)

    def test_workspace_escape_blocked(self):
        h = self.harness(['<tool>{"name": "safe_read_file", "args": {"path": "../../etc/passwd"}}</tool>', "x"])
        h.agent().respond("read", stream=False)
        self.assertIn("outside the workspace", self.backend.prompts[1])


class ContextManagement(Base):
    def test_auto_refresh_archives_to_session_rag(self):
        ev = Recorder()
        h = self.harness(None, ev, model={"context_tokens": 1200, "max_output_tokens": 200},
                         context={"keep_recent_turns": 2})
        agent = h.agent()
        for i in range(12):
            agent.respond(f"fact {i}: the code name for project {i} is zebra{i} " + "padding " * 40, stream=False)
        self.assertTrue(ev.refreshes, "expected an automatic refresh")
        self.assertTrue(h.session.summary)
        self.assertGreater(len(h.session_rag), 0)
        self.assertLessEqual(h.ctx.last.total, h.ctx.budget)
        hits = h.ctx.retrieve("zebra0 code name project")
        self.assertTrue(any("zebra0" in c.text for c in hits))

    def test_manual_refresh_and_injection(self):
        h = self.harness()
        agent = h.agent()
        for i in range(6):
            agent.respond(f"message {i}", stream=False)
        moved = h.ctx.refresh(None, keep=2)
        self.assertEqual(moved, 10)
        self.assertEqual(len(h.session.window), 2)
        h.session.inject("file:notes.txt", "The secret word is pineapple.")
        self.assertIn("pineapple", h.ctx.build("anything").prompt)

    def test_follow_ups_search_with_the_previous_question(self):
        h = self.harness(["One.", "Two."])
        h.project_rag.add_text("billing.md", "Invoices are sent on the first of the month by the billing service.")
        a = h.agent()
        a.respond("when are invoices sent by billing?")
        a.respond("and why?")                                        # nothing to search for on its own
        self.assertIn("billing.md", self.backend.prompts[-1])
        self.assertEqual(h.ctx.retrieval_query("tell me about the deployment pipeline stages please"),
                         "tell me about the deployment pipeline stages please")       # long enough alone

    def test_cag_files_become_notes(self):
        cag = self.ws / ".smallharness" / "cag"
        cag.mkdir(parents=True)
        (cag / "style.md").write_text("Always answer in French.\n")
        (cag / "facts.md").write_text("# facts\nThe server is in Lisbon.\n")
        (cag / "pinned.json").write_text('["style"]')
        h = self.harness()
        self.assertEqual(sorted(n.title for n in h.notes.all()), ["facts", "style"])
        self.assertNotIn("Always answer in French.", h.ctx.build().prompt)    # modes start clean
        self.assertFalse(cag.exists())
        self.assertTrue(cag.with_name("cag.imported").is_dir())

    def test_old_session_indexes_are_removed(self):
        import os
        import time as _time
        state = self.ws / ".smallharness"
        (state / "rag").mkdir(parents=True)
        (state / "sessions").mkdir(parents=True)
        for sid, has_session, age_days in [("orphan", False, 0), ("recent", True, 1), ("old", True, 90)]:
            db = state / "rag" / f"session-{sid}.sqlite"
            db.write_bytes(b"")
            if has_session:
                (state / "sessions" / f"{sid}.jsonl").write_text("")
            old = _time.time() - age_days * 86400
            os.utime(db, (old, old))
        h = self.harness()
        left = sorted(p.name for p in (state / "rag").glob("session-*.sqlite"))
        self.assertEqual(left, sorted(["session-recent.sqlite", f"session-{h.session.id}.sqlite"]))
        self.assertEqual(h.cfg.state.prune_session_indexes(0, h.session.id), 0)   # 0: orphans only

    def test_session_resume(self):
        h = self.harness()
        h.agent().respond("remember me", stream=False)
        h.ctx.refresh(None, keep=0)
        h.agent().respond("second", stream=False)
        from smallharness.context import Session
        s = Session.load(h.cfg.state.sessions, h.session.id)
        self.assertEqual(len(s.history), 4)
        self.assertEqual([t.content for t in s.window][0], "second")
        self.assertEqual(s.summary, h.session.summary)


class Rag(unittest.TestCase):
    def test_bm25_and_reindex(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "src"
            root.mkdir()
            (root / "auth.py").write_text("def login(user, password):\n    check_password(user)\n")
            (root / "math.py").write_text("def add(a, b):\n    return a + b\n")
            store = RagStore(Path(d) / "idx.json")
            self.assertEqual(store.add_path(root, root).files, 2)
            self.assertEqual(store.search("password login")[0].source, "auth.py")
            again = store.add_path(root, root)
            self.assertEqual((again.files, again.chunks, again.unchanged), (0, 0, 2))  # unchanged files skipped
            self.assertEqual(len(RagStore(Path(d) / "idx.json")), len(store))  # persisted

    def test_small_index_still_finds_matches(self):
        # BM25 scores are low in a tiny index; a fixed cutoff used to hide these.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.md").write_text("The deployment uses a blue-green strategy.\nRollback uses the deploy script.\n")
            store = RagStore(None)
            store.add_path(root / "a.md", root)
            for q in ("rollback", "deployed", "strategies"):  # also other word forms
                self.assertEqual([c.source for c in store.search(q)], ["a.md"], q)
            for i in range(3):
                (root / f"n{i}.md").write_text(f"deployment notes {i}\n")
            store.add_path(root, root)
            self.assertEqual(len(store.search("deployment", k=8)), 4)  # a word in every chunk
            self.assertEqual(store.search("kubernetes"), [])

    def test_every_word_form_reaches_one_stem(self):
        from smallharness.context.rag import terms
        for forms in ("complex complexes", "run runs running", "use uses used using", "analysis analyses",
                      "embed embeds embedded embedding", "study studies studied", "agree agreed agreeing"):
            self.assertEqual(len({tuple(terms(w)) for w in forms.split()}), 1, forms)

    def test_camel_case_identifiers_match_whole(self):
        store = RagStore(None)
        store.add_text("bam.md", "The BAM complex has five subunits.\n" * 3)
        store.add_text("bama.md", "BamA is the barrel at the core of it.")
        self.assertEqual(store.search("What is BamA?")[0].source, "bama.md")
        self.assertEqual(store.search("bama")[0].source, "bama.md")

    def test_deleted_files_leave_the_index(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "src"
            root.mkdir()
            (root / "old.py").write_text("def obsolete_helper(): pass\n")
            store = RagStore(None)
            store.add_path(root, root)
            (root / "old.py").unlink()
            self.assertEqual(store.add_path(root, root).removed, 1)
            self.assertEqual(store.search("obsolete helper"), [])

    def test_old_index_is_retokenized(self):
        with tempfile.TemporaryDirectory() as d:
            idx = Path(d) / "idx.json"
            idx.write_text(json.dumps({"chunks": [{"source": "a.md", "start_line": 1, "text": "deployed nightly",
                                                   "tf": {"deployed": 1, "nightly": 1}, "len": 2}],
                                       "sources": {"a.md": 0}}))  # written before stemming
            self.assertEqual(RagStore(idx).search("deploy")[0].source, "a.md")

    def test_indexes_are_ranked_as_one_collection(self):
        from smallharness.context.rag import search
        project, session = RagStore(None), RagStore(None)
        for i in range(300):
            project.add_text(f"doc{i}.md", f"general text {i} about pipelines and servers\n" * 3)
        session.add_text("session:x#1-4", "user: how do we deploy the app\nassistant: run make deploy")
        hits = search([session, project], "how do I deploy", 3)
        self.assertEqual(hits[0].source, "session:x#1-4")          # was outranked by raw BM25 scale
        self.assertEqual(hits[0].score, 1.0)

    def test_two_processes_share_an_index(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = RagStore(Path(d) / "idx.json"), RagStore(Path(d) / "idx.json")   # two connections
            a.add_text("a.md", "the launch code is tangerine")
            self.assertEqual(b.search("tangerine")[0].source, "a.md")  # b sees a's commit
            b.add_text("b.md", "the backup code is grapefruit")
            a.remove("a.md")
            self.assertEqual(sorted(b.sources), ["b.md"])
            self.assertEqual([c.source for c in a.search("code")], ["b.md"])

    def test_threads_read_while_another_writes(self):
        import threading
        store = RagStore(None)
        errors = []

        def write():
            for i in range(200):
                store.add_text(f"f{i % 5}.md", f"deploy step {i}")

        def read():
            try:
                for _ in range(200):
                    store.search("deploy step")
            except Exception as e:      # noqa: BLE001 - any error fails the test
                errors.append(e)
        ts = [threading.Thread(target=write), threading.Thread(target=read)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(store.sources), 5)

    def test_json_index_is_imported_once(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "project.json"
            old.write_text(json.dumps({"version": 2, "chunks": [{"source": "a.md", "start_line": 1,
                                       "text": "deploy nightly", "tf": {"deploy": 1, "nightly": 1}, "len": 2}],
                                       "sources": {"a.md": 5.0}}))
            store = RagStore(Path(d) / "project.sqlite")
            self.assertEqual((store.sources, store.search("nightly")[0].source), ({"a.md": 5.0}, "a.md"))
            self.assertFalse(old.exists())
            self.assertTrue(old.with_suffix(".json.imported").exists())

    def test_chunks_follow_functions_and_headings(self):
        from smallharness.context.rag import chunk_text
        body = "\n".join(f"    x{i} = {i}  # some work here" for i in range(12))
        code = f"import os\n\n\ndef first():\n{body}\n\n\n@cache\ndef second():\n{body}\n\n\nclass Third:\n{body}\n"
        starts = [text.splitlines()[0] for _, text in chunk_text(code, source="m.py")]
        self.assertEqual(starts, ["import os", "@cache", "class Third:"])     # the decorator stays with its def
        md = "# Intro\n" + "word " * 80 + "\n\n## Setup\n" + "word " * 80 + "\n\n## Usage\n" + "word " * 80
        self.assertEqual([text.splitlines()[0] for _, text in chunk_text(md, source="README.md")],
                         ["# Intro", "## Setup", "## Usage"])
        tiny = "def a():\n    pass\n\ndef b():\n    pass\n"
        self.assertEqual(len(chunk_text(tiny, source="t.py")), 1)            # small sections stay together

    def test_chunking_covers_text(self):
        text = "\n".join(f"line {i}" for i in range(500))
        chunks = chunk_text(text, max_chars=300)
        self.assertGreater(len(chunks), 5)
        self.assertIn("line 499", chunks[-1][1])


class Planning(Base):
    def test_parse_plan(self):
        tasks, warns = parse_plan(
            "Here is the plan:\n1. [safe_search_files] Find config\n2) [safe_read_file] Read it (after: 1)\n"
            "3. [magic_tool] Do magic (after: 2, 9)\n- 4. Summarize (after: 3)",
            {"safe_search_files", "safe_read_file"})
        self.assertEqual([t.num for t in tasks], [1, 2, 3, 4])
        self.assertEqual(tasks[1].after, [1])
        self.assertEqual(tasks[2].tool, "none")
        self.assertEqual(tasks[2].after, [2])
        self.assertEqual(len(warns), 2)

    def _plan(self, mode, script):
        h = self.harness(["1. [safe_list_files] List files\n2. [none] Report (after: 1)"] + script)
        plan, _ = create_plan(h, "inventory the workspace")
        return h, PlanRunner(h, plan, mode).run()

    def test_fresh_mode_isolates_tasks(self):
        h, plan = self._plan("fresh", [
            '<tool>{"name": "safe_list_files", "args": {}}</tool>', "DONE: found notes.txt",
            "DONE: workspace has one file"])
        self.assertEqual([t.status for t in plan.tasks], ["done", "done"])
        last = self.backend.prompts[-1]
        self.assertIn("RESULTS OF EARLIER TASKS", last)
        self.assertIn("found notes.txt", last)
        self.assertNotIn("TOOL RESULT: (safe_list_files)", last)  # task 1's tool output is not carried over

    def test_continuous_mode_keeps_context(self):
        _, plan = self._plan("continuous", [
            '<tool>{"name": "safe_list_files", "args": {}}</tool>', "DONE: found notes.txt",
            "DONE: workspace has one file"])
        self.assertEqual(plan.tasks[1].status, "done")
        self.assertIn("TOOL RESULT: (safe_list_files)", self.backend.prompts[-1])

    def test_prompts_guide_tool_choice_and_verdicts(self):
        h, _ = self._plan("fresh", ["DONE: a", "DONE: b"])
        plan_prompt = self.backend.prompts[0]
        self.assertIn("- risky_write_file: Create, write or edit a project file", plan_prompt)
        task_prompt = self.backend.prompts[1]
        self.assertIn("finds nothing is still a result", task_prompt)
        self.assertIn("do not write DONE in the same reply", task_prompt)

    def test_failed_dependency_skips(self):
        _, plan = self._plan("fresh", ["FAILED: cannot list"] * 3)   # the first try and 2 retries (the default)
        self.assertEqual([t.status for t in plan.tasks], ["failed", "skipped"])
        self.assertEqual(plan.tasks[0].attempts, 3)

    def test_saved_plan_is_editable_json(self):
        h, plan = self._plan("fresh", ["DONE: a", "DONE: b"])
        data = json.loads((h.cfg.state.plans / f"{plan.id}.json").read_text())
        self.assertEqual(data["tasks"][0]["status"], "done")


class Resources(unittest.TestCase):
    def test_capacity_math(self):
        with tempfile.NamedTemporaryFile() as f:
            f.truncate(200 * 2**20)  # 200 MB sparse "model"
            rep = capacity(f.name, 4096, {"reserve_ram_mb": 0, "overhead_factor": 1.0,
                                          "kv_cache_mb": 100, "max_instances": 64})
            self.assertEqual(rep.footprint_mb, 300)
            self.assertEqual(rep.max_instances, min(64, rep.usable_mb // 300))

    def test_kv_estimate_scales(self):
        self.assertLess(estimate_kv_cache_mb(500, 4096), estimate_kv_cache_mb(2500, 4096))


class Forwarding(unittest.TestCase):
    def test_every_event_passes_unless_dropped(self):
        from smallharness.agent import ForwardingEvents
        got = []

        class Inner(Events):
            def __getattribute__(self, name):
                if name.startswith("on_"):
                    return lambda *a: got.append(name)
                return super().__getattribute__(name)

            def permit(self, tool, args):
                got.append("permit")
                return True
        names = [n for n in vars(Events) if n.startswith("on_")]
        fw = ForwardingEvents(Inner(), drop=("on_text",))
        for n in names:
            getattr(fw, n)(*([None] * (getattr(Events, n).__code__.co_argcount - 1)))
        self.assertTrue(fw.permit(None, {}))
        self.assertEqual(got, [n for n in names if n != "on_text"] + ["permit"])
        self.assertIn("on_stats", got)


if __name__ == "__main__":
    unittest.main()
