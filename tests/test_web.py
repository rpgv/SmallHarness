"""The visual interface's server (chat, research, notes, planner, recipes, relics), against the mock model."""
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from smallharness.agent import Events, Harness
from smallharness.backends import MockBackend
from smallharness.config import Config
from smallharness.web.server import make_server


class Web(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()
        (self.ws / "readme.txt").write_text("The launch code is tangerine.\n")
        cfg = Config.load(self.ws, overrides={"model": {"backend": "mock"}})
        self.backend = MockBackend()
        self.h = Harness(cfg, Events(), backend_factory=lambda: self.backend)
        self.addCleanup(self.h.close)
        self.server, self.url = make_server(self.h, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = self.url.rstrip("/")
        self.app = self.server.RequestHandlerClass.app

    def req(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, method=method, data=data)
        if body is not None:
            r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=20) as res:
                text = res.read().decode()
                ctype = res.headers["Content-Type"]
                status = res.status
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
        if "ndjson" in ctype:
            return status, [json.loads(line) for line in text.splitlines() if line]
        return status, json.loads(text) if "json" in ctype else text

    def wait_for_permit(self):
        for _ in range(100):
            if self.app.pending:
                return next(iter(self.app.pending))
            time.sleep(0.05)
        self.fail("no permission request")

    # ------------------------------------------------------------- access
    def test_page_and_access_rules(self):
        self.assertNotIn("key", self.url)
        status, page = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("SmallHarness", page)
        for tab in ("Chat", "Research", "Planner", "Recipes"):
            self.assertIn(f">{tab}</button>", page)
        self.assertEqual(self.req("GET", "/app.js")[0], 200)
        self.assertIn("[hidden] { display: none !important; }", self.req("GET", "/app.css")[1])
        self.assertEqual(self.req("GET", "/../server.py")[0], 404)
        self.assertEqual(self.req("GET", "/api/state")[1]["version"], "1.0.0")     # no key needed
        self.assertEqual(self.req("GET", "/api/state", headers={"Host": "evil.example:80"})[0], 403)  # rebinding
        other = {"Origin": "http://evil.example"}
        self.assertEqual(self.req("POST", "/api/notes", {"text": "x"}, headers=other)[0], 403)   # another site
        self.assertEqual(self.req("POST", "/api/notes", raw=b'{"text": "x"}',
                                  headers={"Content-Type": "text/plain"})[0], 403)             # not JSON
        own = {"Origin": self.base}
        self.assertEqual(self.req("POST", "/api/notes", {"text": "from the page"}, headers=own)[0], 200)

    # ------------------------------------------------------------ research
    def test_knowledge_base_works_without_a_model(self):
        _, events = self.req("POST", "/api/research/files", {"path": "readme.txt"})
        self.assertIn("indexed 1 file", events[-1]["result"])
        status, note = self.req("POST", "/api/notes", {"text": "Buy tangerines #shopping"})
        self.assertEqual(status, 200, note)
        _, r = self.req("GET", "/api/research")
        self.assertEqual({x["source"] for x in r["library"]}, {"readme.txt", "note:1"})
        _, r = self.req("PUT", "/api/research/scope", {"sources": ["readme.txt"]})
        self.assertEqual(r["scope"], ["readme.txt"])
        self.assertEqual(self.req("PUT", "/api/research/scope", {"sources": ["nope.md"]})[0], 404)
        _, found = self.req("POST", "/api/research/find", {"query": "launch code"})
        self.assertEqual(found["refs"], [1])
        self.assertEqual(found["research"]["references"][0]["source"], "readme.txt")
        _, r = self.req("POST", "/api/research/remove", {"source": "note:1"})
        self.assertNotIn("note:1", {x["source"] for x in r["library"]})
        self.assertFalse(self.req("GET", "/api/state")[1]["model"]["loaded"])

    def test_asking_cites_and_attaches(self):
        self.req("POST", "/api/research/files", {"path": "readme.txt"})
        _, events = self.req("POST", "/api/research/ask", {"question": "what is the launch code?"})
        kinds = [e["type"] for e in events]
        self.assertIn("loaded", kinds)
        self.assertIn("stats", kinds)                                  # the speed readout's numbers
        done = events[-1]
        self.assertEqual(done["answer"]["text"], "From the excerpts: see [1].")
        self.assertEqual(done["answer"]["refs"], [1])
        self.assertTrue(Path(done["research"]["bibliography"]).is_file())
        self.assertNotIn("ATTACHED", self.backend.prompts[-1])
        _, r = self.req("PUT", "/api/research/answers/1/attach", {"attached": True})
        self.assertTrue(r["answers"][0]["attached"])
        self.req("POST", "/api/research/ask", {"question": "and the launch code again?"})
        self.assertIn("ATTACHED", self.backend.prompts[-1])
        self.req("PUT", "/api/research/answers/1/attach", {"attached": False})
        self.assertEqual(self.req("PUT", "/api/research/answers/9/attach", {})[0], 404)
        _, fresh = self.req("POST", "/api/research/new")
        self.assertEqual((fresh["answers"], fresh["references"]), ([], []))

    def test_uploads(self):
        status, r = self.req("POST", "/api/research/upload?name=manual.md", raw=b"Press the red button to stop.\n",
                             headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200, r)
        self.assertIn(".smallharness/library/manual.md", {x["source"] for x in r["research"]["library"]})
        self.assertEqual(self.req("POST", "/api/research/upload?name=x.md", raw=b"text",
                                  headers={"Content-Type": "application/json"})[0], 403)    # must be binary

    # ------------------------------------------------------------- planner
    def test_planner(self):
        status, plan = self.req("POST", "/api/plans", {"goal": "inventory"})
        self.assertEqual(status, 200, plan)
        self.assertEqual(len(plan["tasks"]), 2)
        _, events = self.req("POST", f"/api/plans/{plan['id']}/expand", {"times": 2, "parallel": False})
        self.assertEqual(sum(e["type"] == "step" for e in events), 4)
        tasks = events[-1]["plan"]["tasks"]
        self.assertEqual([t["depth"] for t in tasks], [2, 2])
        self.assertIn("Use the suggested tool", tasks[0]["detail"])
        self.assertIn("   Use the suggested tool", events[-1]["plan"]["text"])           # details, indented
        _, events = self.req("POST", f"/api/plans/{plan['id']}/run", {"mode": "fresh"})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual([t["status"] for t in self.req("GET", f"/api/plans/{plan['id']}")[1]["tasks"]], ["done", "done"])
        self.assertEqual(self.req("POST", f"/api/plans/{plan['id']}/run", {"mode": "heartbeat"})[0], 400)
        self.assertEqual(self.req("POST", f"/api/plans/{plan['id']}/expand", {"steps": "all"})[0], 400)

    def test_plans_drafted_and_edited_by_hand(self):
        status, plan = self.req("POST", "/api/plans", {"goal": "by hand",
                                                       "text": "[safe_list_files] List\n   only docs/\n[none] Report (after: 1)"})
        self.assertEqual(status, 200, plan)
        self.assertEqual(plan["tasks"][0]["detail"], "only docs/")
        self.assertFalse(self.req("GET", "/api/state")[1]["model"]["loaded"])          # no model needed
        status, edited = self.req("PUT", f"/api/plans/{plan['id']}", {"text": plan["text"] + "\n3. [none] Done"})
        self.assertEqual((status, edited["goal"], len(edited["tasks"])), (200, "by hand", 3))
        self.assertEqual(self.req("PUT", "/api/plans/nope", {"text": "1. x"})[0], 404)

    def test_risky_tool_asks_the_page(self):
        _, plan = self.req("POST", "/api/plans", {"goal": "write", "text": "[risky_write_file] Write x.txt"})
        self.backend.script.extend(['<tool>{"name": "risky_write_file", "args": {"path": "x.txt", "content": "hi"}}</tool>',
                                    "DONE: wrote it"])
        out = {}
        t = threading.Thread(target=lambda: out.update(
            events=self.req("POST", f"/api/plans/{plan['id']}/run", {"mode": "fresh"})[1]))
        t.start()
        pid = self.wait_for_permit()
        self.assertEqual(self.req("POST", "/api/research/ask", {"question": "x"})[0], 409)   # one at a time
        self.req("POST", "/api/permit", {"id": pid, "allow": True})
        t.join(20)
        self.assertEqual(next(e for e in out["events"] if e["type"] == "permit")["tool"], "risky_write_file")
        self.assertEqual((self.ws / "x.txt").read_text().strip(), "hi")

    def test_stop_ends_a_plan_run(self):
        _, plan = self.req("POST", "/api/plans", {"goal": "g", "text": "[none] First\n[none] Second"})

        def slow(prompt, max_tokens, stop=None):           # a long answer, streamed
            for _ in range(100):
                time.sleep(0.05)
                yield "word "
        self.backend.stream = slow
        out = {}
        t = threading.Thread(target=lambda: out.update(
            events=self.req("POST", f"/api/plans/{plan['id']}/run", {"mode": "fresh"})[1]))
        t.start()
        time.sleep(0.5)
        self.assertEqual(self.req("POST", "/api/stop", {})[0], 200)
        t.join(20)
        self.assertEqual(out["events"][-1]["type"], "stopped")
        _, detail = self.req("GET", f"/api/plans/{plan['id']}")
        self.assertEqual([t["status"] for t in detail["tasks"]], ["pending", "pending"])
        self.assertFalse(self.app.busy.locked())

    def test_a_run_outlives_the_page(self):
        """A plan run goes on when the page goes away, and any page can follow it again."""
        _, plan = self.req("POST", "/api/plans", {"goal": "g", "text": "[none] First\n[none] Second"})
        asked, release, real = threading.Event(), threading.Event(), self.backend.generate

        def held(prompt, max_tokens, stop=None):
            asked.set()
            release.wait(10)
            return real(prompt, max_tokens, stop)
        self.backend.generate = held
        r = urllib.request.Request(self.base + f"/api/plans/{plan['id']}/run", method="POST",
                                   data=b'{"mode": "fresh"}', headers={"Content-Type": "application/json"})
        res = urllib.request.urlopen(r, timeout=20)
        job = json.loads(res.readline())
        res.close()                                                                      # the page goes away
        self.assertEqual(job["type"], "job")
        self.assertTrue(asked.wait(10))
        self.assertEqual(self.req("GET", "/api/state")[1]["job"]["id"], job["id"])
        _, live = self.req("GET", f"/api/plans/{plan['id']}")
        self.assertEqual(live["tasks"][0]["status"], "running")                           # live, not as saved
        self.assertEqual(self.req("PUT", f"/api/plans/{plan['id']}/steps", {"done": True})[0], 409)
        release.set()
        _, events = self.req("GET", f"/api/jobs/{job['id']}/events")                      # follows to the end
        self.assertEqual((events[0]["type"], events[-1]["type"]), ("job", "done"))
        self.assertTrue(events[-1]["plan"]["finished"])
        self.assertEqual([e["seq"] for e in events[1:]], list(range(len(events) - 1)))
        _, tail = self.req("GET", f"/api/jobs/{job['id']}/events?since=3")              # where a page left off
        self.assertEqual(tail[1:], events[4:])
        self.assertTrue(self.req("GET", "/api/state")[1]["job"]["finished"])
        self.assertEqual(self.req("GET", "/api/jobs/nope/events")[0], 404)
        self.assertFalse(self.app.busy.locked())

    def test_plan_steps_ticked_and_plans_deleted(self):
        _, plan = self.req("POST", "/api/plans", {"goal": "g", "text": "[none] First\n[none] Second (after: 1)"})
        pid = plan["id"]
        _, p = self.req("PUT", f"/api/plans/{pid}/steps", {"steps": [1], "done": True})
        self.assertEqual([t["status"] for t in p["tasks"]], ["done", "pending"])
        _, p = self.req("PUT", f"/api/plans/{pid}/steps", {"done": True})                  # every step
        self.assertTrue(p["finished"])
        _, p = self.req("PUT", f"/api/plans/{pid}/steps", {"steps": [2], "done": False})   # run it again
        self.assertEqual([t["status"] for t in p["tasks"]], ["done", "pending"])
        self.assertEqual(self.req("PUT", f"/api/plans/{pid}/steps", {"steps": [9]})[0], 404)
        self.assertEqual(self.req("PUT", f"/api/plans/{pid}/steps", {"steps": "all"})[0], 400)
        self.assertEqual(self.req("DELETE", f"/api/plans/{pid}")[0], 200)
        self.assertEqual(self.req("GET", f"/api/plans/{pid}")[0], 404)
        self.assertEqual(self.req("GET", "/api/plans")[1]["plans"], [])
        self.assertEqual(self.req("DELETE", f"/api/plans/{pid}")[0], 404)

    def test_plan_writes_are_relics(self):
        _, plan = self.req("POST", "/api/plans", {"goal": "write", "text": "[risky_write_file] Write out/x.txt"})
        self.backend.script.extend(['<tool>{"name": "risky_write_file", "args": {"path": "out/x.txt", "content": "hi"}}</tool>',
                                    "DONE: wrote it"])
        t = threading.Thread(target=lambda: self.req("POST", f"/api/plans/{plan['id']}/run", {"mode": "fresh"}))
        t.start()
        self.req("POST", "/api/permit", {"id": self.wait_for_permit(), "allow": True})
        t.join(20)
        _, r = self.req("GET", "/api/relics")
        self.assertEqual([(x["path"], x["by"], x["exists"]) for x in r["relics"]],
                         [("out/x.txt", [f"plan:{plan['id']}"], True)])
        self.assertEqual(self.req("GET", "/api/relics/file?path=out/x.txt")[1]["text"], "hi")
        _, r = self.req("PUT", "/api/relics/file", {"path": "out/x.txt", "text": "edited"})
        self.assertEqual((self.ws / "out/x.txt").read_text(), "edited")
        self.assertIn("edited", r["relics"][0]["by"])
        self.assertEqual(self.req("PUT", "/api/relics/file", {"path": "readme.txt", "text": "x"})[0], 404)  # not a relic
        self.assertEqual(self.req("GET", "/api/relics/file?path=../etc/passwd")[0], 422)
        _, r = self.req("DELETE", "/api/relics/file?path=out/x.txt")
        self.assertEqual(r["relics"], [])
        self.assertTrue((self.ws / "out/x.txt").exists())                                 # the file stays

    # ---------------------------------------------------------------- chat
    def test_chat(self):
        _, c = self.req("POST", "/api/chats")
        cid = c["id"]
        _, events = self.req("POST", f"/api/chats/{cid}/send", {"text": "/read readme.txt\nwhat is the code?"})
        self.assertEqual(events[0]["type"], "job")
        added = [e["message"] for e in events if e["type"] == "message"]
        self.assertEqual([(m["role"], m["num"]) for m in added], [("user", 1), ("tool", 2), ("assistant", 3)])
        self.assertIn("tangerine", added[1]["text"])
        self.assertNotIn("TOOLS:", self.backend.prompts[-1])
        self.assertEqual(events[-1]["chat"]["title"], "/read readme.txt")
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/send", {"text": "/nope"})[0], 422)   # before any job
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/send", {"text": "x", "thread": 99})[0], 404)
        _, events = self.req("POST", f"/api/chats/{cid}/send", {"text": "more please", "thread": 3})
        self.assertEqual(events[-1]["chat"]["threads"], {"3": 2})
        _, chat = self.req("PUT", f"/api/chats/{cid}/messages/3/pin", {"pinned": True})
        self.assertEqual(chat["pinned"], [3])
        _, chat = self.req("POST", f"/api/chats/{cid}/rewind", {"to": 3, "thread": 3})
        self.assertEqual((chat["cleared"], chat["threads"]), (2, {}))
        _, chat = self.req("POST", f"/api/chats/{cid}/rewind", {"to": 0})
        self.assertEqual(chat["messages"], [])
        _, chat = self.req("PUT", f"/api/chats/{cid}", {"title": "renamed"})
        _, listing = self.req("GET", "/api/chats")
        self.assertEqual([x["title"] for x in listing["chats"]], ["renamed"])
        self.assertIn("agent", [x["name"] for x in listing["commands"]])
        self.assertEqual(self.req("DELETE", f"/api/chats/{cid}")[0], 200)
        self.assertEqual(self.req("GET", f"/api/chats/{cid}")[0], 404)

    # ------------------------------------------------------------- recipes
    def test_recipes(self):
        status, r = self.req("PUT", "/api/recipes/calc", {"text": "add(a, b) -> int\n\nsub(a, b) -> int", "target": "calc.py"})
        self.assertEqual(status, 200, r)
        self.assertEqual((r["target"], r["mode"], len(r["sections"])), ("calc.py", "code", 2))
        self.assertEqual(self.req("PUT", "/api/recipes/bad", {"text": "x"})[0], 422)            # no target
        self.assertEqual(self.req("GET", "/api/recipes/missing")[0], 404)
        out = {}
        t = threading.Thread(target=lambda: out.update(events=self.req("POST", "/api/recipes/calc/run", {})[1]))
        t.start()
        pid = self.wait_for_permit()
        self.assertFalse((self.ws / "calc.py").exists())                                  # asked before writing
        job = self.req("GET", "/api/state")[1]["job"]
        with urllib.request.urlopen(self.base + f"/api/jobs/{job['id']}/events", timeout=20) as res:
            asked = next(json.loads(line) for line in res if b'"permit"' in line)
        self.assertEqual(asked["id"], pid)                                                # a page joining is asked too
        self.req("POST", "/api/permit", {"id": pid, "allow": True})
        t.join(20)
        events = out["events"]
        self.assertEqual(sum(e["type"] == "permit" for e in events), 1)                   # once
        _, replay = self.req("GET", f"/api/jobs/{job['id']}/events")
        self.assertEqual([e["type"] for e in replay if "permit" in e["type"]], ["permit_end"])  # answered: not asked again
        self.assertEqual(events[-1]["written"], [1, 2])
        self.assertIn("def add", (self.ws / "calc.py").read_text())
        _, r = self.req("PUT", "/api/recipes/intro", {"text": "Purpose\n\nAudience", "target": "intro.md"})
        self.assertEqual(r["mode"], "text")
        _, r = self.req("PUT", "/api/recipes/intro", {"text": "Purpose\n\nAudience", "target": "intro.md", "mode": "code"})
        self.assertEqual(r["mode"], "code")                                                # the toggle
        self.assertEqual([x["path"] for x in self.req("GET", "/api/relics")[1]["relics"]], ["calc.py"])
        _, r = self.req("PUT", "/api/recipes/calc/sections", {"sections": [2], "done": False})   # untick: write again
        self.assertEqual([s["state"] for s in r["sections"]], ["done", "pending"])
        _, r = self.req("PUT", "/api/recipes/calc/sections", {"sections": [2], "done": True})    # and tick back
        self.assertEqual([s["state"] for s in r["sections"]], ["done", "done"])
        _, r = self.req("PUT", "/api/recipes/calc/sections", {"done": False})                    # every section
        self.assertEqual([s["state"] for s in r["sections"]], ["pending", "pending"])
        self.assertEqual(self.req("PUT", "/api/recipes/calc/sections", {"sections": [7]})[0], 404)
        self.assertEqual(self.req("PUT", "/api/recipes/intro/sections", {"sections": [1], "done": True})[0], 422)  # never written
        self.assertEqual(self.req("DELETE", "/api/recipes/calc")[0], 200)
        self.assertTrue((self.ws / "calc.py").exists())                                   # the code stays

    def test_code_library(self):
        self.req("PUT", "/api/recipes/calc", {"text": "add(a, b) -> int\n\nsub(a, b) -> int", "target": "calc.py"})
        self.h.cfg.data["recipe"]["library"] = True
        from smallharness.recipe import RecipeRunner, load_saved
        RecipeRunner(self.h, load_saved(self.h.cfg, "calc")).run(permit=lambda: True)
        _, r = self.req("GET", "/api/recipes/calc")
        self.assertEqual(r["library"], "python")
        self.assertTrue(all(s["snippet"] for s in r["sections"]))
        self.assertIn("def add", r["sections"][0]["code"])                  # read from the snippet file
        _, langs = self.req("GET", "/api/snippets")
        self.assertEqual(langs["languages"], [{"key": "python", "snippets": 2}])
        _, found = self.req("GET", "/api/snippets/python?q=add")
        sid = found["snippets"][0]["id"]
        self.assertTrue(sid.startswith("add-"))
        self.assertIn("def add", self.req("GET", f"/api/snippets/python/{sid}")[1]["code"])
        self.assertEqual(self.req("DELETE", f"/api/snippets/python/{sid}")[0], 200)
        self.assertEqual(self.req("GET", f"/api/snippets/python/{sid}")[0], 404)
        self.assertEqual(self.req("GET", "/api/snippets/klingon")[0], 404)
        self.req("PUT", "/api/recipes/calc/sections", {"sections": [2], "done": False})
        calls = len(self.backend.prompts)
        RecipeRunner(self.h, load_saved(self.h.cfg, "calc")).run(permit=lambda: True)
        self.assertGreater(len(self.backend.prompts), calls)      # unticked: written anew, not reused
        again = load_saved(self.h.cfg, "calc").sections[1]
        self.assertEqual((again.status, again.redo, again.reused), ("done", False, False))
        _, r = self.req("PUT", "/api/recipes/intro", {"text": "Purpose", "target": "intro.md"})
        self.assertIsNone(r["library"])                                        # text mode: no library

    # ----------------------------------------------------- models, settings
    def test_models_settings_errors(self):
        _, models = self.req("GET", "/api/models")
        self.assertIn("available", models)
        _, cfg = self.req("GET", "/api/config")
        self.assertIn("model.backend", {r["key"] for r in cfg["settings"]})
        status, r = self.req("POST", "/api/config", {"values": {"research.k": "4"}})
        self.assertEqual(status, 200, r)
        self.assertEqual(self.h.cfg["research"]["k"], 4)
        self.assertEqual(self.req("POST", "/api/config", {"values": {"nope.key": "1"}})[0], 400)
        tools = {t["name"] for t in self.req("GET", "/api/tools")[1]["tools"]}
        self.assertEqual(tools, {"safe_read_file", "safe_list_files", "safe_search_files",
                                 "risky_write_file", "risky_run_command"})               # the planner's tools
        self.assertEqual(self.req("GET", "/api/notes/99")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/models/nope")[0], 404)
        for gone in ("/api/chat", "/api/session", "/api/sessions", "/api/rag", "/api/cag"):
            self.assertEqual(self.req("GET", gone)[0], 404, gone)

    def test_load_flag_loads_before_serving(self):
        from smallharness import cli
        with mock.patch("smallharness.web.server.ThreadingHTTPServer.serve_forever", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()) as out:
            cli.serve_web(self.h, "127.0.0.1", 0, open_browser=False)
            self.assertFalse(self.h.pool.instances)                           # default: on first use
            cli.serve_web(self.h, "127.0.0.1", 0, open_browser=False, load=True)
        self.assertTrue(self.h.pool.instances)
        self.assertIn("loaded in", out.getvalue())
        self.assertNotIn("key", out.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
