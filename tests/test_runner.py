"""Running relics from the web page: the suggested command, and runs that stream and stop."""
import os
import time

from smallharness.errors import Invalid, NotFound
from smallharness.runner import Runner, suggest

from helpers import WorkspaceTest


class Suggest(WorkspaceTest):
    def write(self, rel, text, mode=0o644):
        p = self.ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        p.chmod(mode)

    def test_by_type_shebang_and_venv(self):
        self.write("a.py", "print(1)\n")
        self.write("dir/my script.sh", "echo hi\n")
        self.write("b.js", "")
        self.write("c.c", "")
        self.write("notes.md", "# x")
        self.assertEqual(suggest(self.ws, "a.py"), "python3 a.py")
        self.assertEqual(suggest(self.ws, "dir/my script.sh"), "bash 'dir/my script.sh'")
        self.assertEqual(suggest(self.ws, "b.js"), "node b.js")
        self.assertEqual(suggest(self.ws, "c.c"), "cc c.c -o /tmp/smallharness-c && /tmp/smallharness-c")
        self.assertEqual(suggest(self.ws, "notes.md"), "")
        self.write("tool", "#!/usr/bin/env python3\nprint(1)\n")
        self.assertEqual(suggest(self.ws, "tool"), "python3 tool")
        self.write("run", "#!/bin/sh\necho\n", 0o755)
        self.assertEqual(suggest(self.ws, "run"), "./run")
        (self.ws / ".venv" / "bin").mkdir(parents=True)
        (self.ws / ".venv" / "bin" / "python").write_text("")
        self.assertEqual(suggest(self.ws, "a.py"), ".venv/bin/python a.py")


class Runs(WorkspaceTest):
    def test_output_streams_and_the_exit_code_comes_last(self):
        r = Runner(self.ws, {}).start("echo one; echo two >&2; exit 3")
        events = list(r.follow())
        self.assertEqual("".join(v for k, v in events if k == "out"), "one\ntwo\n")
        self.assertEqual(events[-1], ("end", 3))

    def test_runs_in_the_workspace_and_stop_ends_the_group(self):
        runner = Runner(self.ws, {})
        self.assertIn(str(self.ws), "".join(v for k, v in runner.start("pwd").follow() if k == "out"))
        r = runner.start("sleep 30 & sleep 30; echo never")
        time.sleep(0.2)
        runner.get(r.id).stop()
        events = list(r.follow())
        self.assertNotEqual(events[-1][1], 0)
        self.assertEqual(r.stopped, "stopped")
        with self.assertRaises(ProcessLookupError):
            os.killpg(r.proc.pid, 0)                                   # nothing it started is left
        with self.assertRaises(NotFound):
            runner.get("nope")
        with self.assertRaises(Invalid):
            runner.start("  ")

    def test_timeout(self):
        r = Runner(self.ws, {"run_timeout": 0.3}).start("sleep 10")
        list(r.follow())
        self.assertIn("stopped after", r.stopped)
