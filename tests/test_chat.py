"""Chat mode: tools only on command, threads, pins (CAG), rewinding, and folding the
context into a summary as it fills."""
import json

from smallharness.agent import Events
from smallharness.chat import ChatStore, parse, tool_args
from smallharness.errors import Invalid, NotFound

from helpers import WorkspaceTest


class Allow(Events):
    """Allows every risky tool, and keeps what it was asked."""

    def __init__(self):
        self.asked, self.notices = [], []

    def permit(self, tool, args):
        self.asked.append(tool.name)
        return True

    def on_notice(self, msg):
        self.notices.append(msg)


class Chat(WorkspaceTest):
    def setUp(self):
        super().setUp()
        (self.ws / "notes.txt").write_text("The launch code is tangerine.\n")

    def chat(self, script=None, **overrides):
        self.h = self.harness(script, **overrides)
        return self.h.chats.new()

    def prompt(self):
        return self.backend.prompts[-1]

    # ------------------------------------------------------------ parsing
    def test_commands_lead_the_message(self):
        self.assertEqual(parse("hello"), ([], "hello"))
        self.assertEqual(parse("/read a.py\n/ls\nwhat is in it?"), ([("read", "a.py"), ("ls", "")], "what is in it?"))
        self.assertEqual(parse("/agent fix it\nand test it"), ([("agent", "fix it\nand test it")], ""))
        self.assertEqual(parse("why /read here?"), ([], "why /read here?"))      # only at the start
        h = self.harness()
        read, run = h.tools.get("safe_read_file"), h.tools.get("risky_run_command")
        self.assertEqual(tool_args(read, "a.py 3 10"), {"path": "a.py", "start": "3", "lines": "10"})
        self.assertEqual(tool_args(read, "path=a.py lines=5"), {"path": "a.py", "lines": "5"})
        self.assertEqual(tool_args(read, '{"path": "a.py"}'), {"path": "a.py"})
        self.assertEqual(tool_args(run, "ls -la | head"), {"command": "ls -la | head"})
        with self.assertRaises(Invalid):
            tool_args(read, "a b c d")

    # -------------------------------------------------------------- tools
    def test_no_tools_without_a_command(self):
        chat = self.chat(['<tool>{"name": "safe_read_file", "args": {"path": "notes.txt"}}</tool>'])
        added = chat.send("what is the launch code?")
        self.assertNotIn("TOOLS:", self.prompt())                     # no tool list offered
        self.assertIn("cannot read files", self.prompt())
        self.assertEqual([m.role for m in added], ["user", "assistant"])
        self.assertIn("<tool>", added[-1].text)                        # a call in a reply is just text
        self.assertNotIn("tangerine", added[-1].text)

    def test_a_command_runs_the_tool_and_the_question_sees_it(self):
        chat = self.chat()
        added = chat.send("/read notes.txt\nwhat is the launch code?")
        self.assertEqual([(m.role, m.kind) for m in added], [("user", "chat"), ("tool", "tool"), ("assistant", "chat")])
        self.assertTrue(added[1].meta["ok"])
        self.assertEqual(added[1].meta["for"], added[0].num)
        self.assertIn("tangerine", self.prompt())
        self.assertIn("what is the launch code?", self.prompt())
        calls = len(self.backend.prompts)
        added = chat.send("/ls *.txt")                                 # a command alone: no model call
        self.assertEqual(len(self.backend.prompts), calls)
        self.assertIn("notes.txt", added[-1].text)
        with self.assertRaises(Invalid):                               # unknown: nothing is kept
            chat.send("/frobnicate x")
        self.assertEqual(len(chat.messages), 5)
        added = chat.send("/read missing.txt\nsummarize it")           # a failing tool: no answer is made up
        self.assertFalse(added[1].meta["ok"])
        self.assertEqual(len(self.backend.prompts), calls + 1)         # (it still answers, seeing the error)

    def test_errors_stop_the_message(self):
        chat = self.chat()
        added = chat.send("/tool nope\nthen answer")
        self.assertEqual([m.kind for m in added], ["chat", "error"])
        self.assertEqual(added[1].seen, "")                            # not part of the context
        self.assertEqual(self.backend.prompts, [])

    def test_write_saves_the_last_code_block_as_a_relic(self):
        ev = Allow()
        chat = self.chat(["Here:\n```python\nprint('hi')\n```"])
        chat.send("write hello world", events=ev)
        added = chat.send("/write hello.py", events=ev)
        self.assertEqual(ev.asked, ["risky_write_file"])               # risky still asks
        self.assertEqual((self.ws / "hello.py").read_text(), "print('hi')\n")
        self.assertNotIn("content", added[-1].meta["args"])
        self.assertEqual([r["by"] for r in self.h.relics.all()], [[f"chat:{chat.id}"]])

    def test_agent_lends_tools_for_one_message(self):
        chat = self.chat(['<tool>{"name": "safe_read_file", "args": {"path": "notes.txt"}}</tool>',
                          "The code is tangerine."])
        added = chat.send("/agent find the launch code", events=Allow())
        self.assertIn("TOOLS:", self.backend.prompts[0])
        answer = added[-1]
        self.assertEqual((answer.kind, answer.text), ("agent", "The code is tangerine."))
        self.assertEqual(answer.meta["steps"][0]["tool"], "safe_read_file")
        self.assertIn("tangerine", answer.meta["steps"][1]["text"])
        self.assertIn("notes.txt:\n", self.backend.prompts[1])           # it had the result while working
        chat.send("thanks")
        self.assertNotIn("TOOLS:", self.prompt())                       # off again
        self.assertNotIn("notes.txt:\n", self.prompt())                 # the tool result was not kept
        self.assertIn("find the launch code", self.prompt())            # the task and answer were

    def test_modes_by_command(self):
        ev = Allow()
        chat = self.chat()
        self.h.project_rag.add_path(self.ws / "notes.txt", self.ws)
        added = chat.send("/ask what is the launch code?", events=ev)
        self.assertEqual(added[-1].kind, "research")
        self.assertEqual(added[-1].meta["refs"][0]["source"], "notes.txt")
        added = chat.send("/note Ship on Friday #ops\n/notes friday")
        self.assertIn("saved note 1", added[1].text)
        self.assertIn("Ship on Friday", added[2].text)
        added = chat.send("/plan tidy the docs", events=ev)
        plan_id = added[-1].meta["plan"]
        self.assertIn("List the workspace files", added[-1].text)
        added = chat.send("/runplan", events=ev)
        self.assertEqual(added[-1].meta["plan"], plan_id)
        self.assertTrue(added[-1].meta["finished"])
        self.assertTrue(any(n.startswith("step 1") for n in ev.notices))
        added = chat.send("/help\n/tools")
        self.assertEqual([m.kind for m in added[1:]], ["info", "info"])
        self.assertIn("/agent TASK", added[1].text)
        self.assertIn("safe_read_file", added[2].text)

    # ---------------------------------------------------- threads and pins
    def test_a_thread_has_the_smaller_context(self):
        chat = self.chat()
        chat.send("tell me about apples")                               # 1, answer 2
        chat.send("now about bicycles")                                 # 3, answer 4
        added = chat.send("are they red?", thread=2)
        self.assertEqual({m.thread for m in added}, {2})
        self.assertIn("apples", self.prompt())
        self.assertNotIn("bicycles", self.prompt())
        self.assertIn("thread on message #2", self.prompt())
        added = chat.send("and green?", thread=added[-1].num)           # inside a thread: the same thread
        self.assertEqual(added[0].thread, 2)
        self.assertIn("are they red?", self.prompt())
        chat.send("back to the main line")
        self.assertNotIn("are they red?", self.prompt())                # threads stay out of it
        self.assertEqual(chat.to_dict()["threads"], {2: 4})

    def test_pins_reach_threads_and_outlive_folding(self):
        chat = self.chat()
        chat.send("my favourite colour is ultramarine")                 # 1, 2
        chat.send("and my dog is called Biscuit")                       # 3, 4
        chat.pin(1)
        chat.send("what is my dog called?", thread=4)
        self.assertIn("[pinned #1]", self.prompt())
        self.assertIn("ultramarine", self.prompt())
        chat.send("hi")
        self.assertNotIn("[pinned #1]", self.prompt())                  # still in the window: not twice
        chat.pin(1, False)
        info = chat.send("/help")[-1]
        with self.assertRaises(Invalid):
            chat.pin(info.num)                                          # not in the context
        with self.assertRaises(NotFound):
            chat.pin(99)

    # ------------------------------------------------------------- rewind
    def test_rewind_clears_messages_threads_and_pins(self):
        chat = self.chat()
        chat.send("one")                                                # 1, 2
        chat.send("two")                                                # 3, 4
        chat.send("about two", thread=4)                                # 5, 6
        chat.pin(4)
        self.assertEqual(chat.rewind(6, thread=4), 0)
        self.assertEqual(chat.rewind(4, thread=4), 2)                   # the thread only
        self.assertEqual([m.num for m in chat.messages], [1, 2, 3, 4])
        chat.send("about two again", thread=4)                          # 5, 6
        self.assertEqual(chat.rewind(2), 4)                             # everything after #2
        self.assertEqual([m.num for m in chat.messages], [1, 2])
        self.assertEqual(chat.to_dict()["pinned"], [])
        chat.send("three")
        self.assertNotIn("two", self.prompt())
        self.assertEqual([m.num for m in chat.messages], [1, 2, 3, 4])  # numbers go on from the point
        with self.assertRaises(Invalid):
            chat.rewind(1, thread=4)
        self.assertEqual(chat.rewind(0), 4)

    # ------------------------------------------------- folding and storage
    def test_the_context_folds_and_rewind_undoes_it(self):
        chat = self.chat(model={"context_tokens": 900, "max_output_tokens": 100},
                         context={"keep_recent_turns": 2})
        filler = " ".join(["word"] * 120)
        for i in range(6):
            chat.send(f"fact {i}: the zeppelin{i} is parked. {filler}")
        self.assertIn("0", chat.folds)
        self.assertIn("SUMMARY OF EARLIER CONVERSATION", self.prompt())
        self.assertNotIn("zeppelin0 is parked", self.prompt().split("CONVERSATION:")[-1])
        source = chat.folds["0"][0].source
        self.assertIn(source, chat.archive.sources)
        added = chat.send("/recall zeppelin0")
        self.assertIn("zeppelin0", added[-1].text)
        first_fold = chat.folds["0"][0].last
        chat.rewind(first_fold - 1)
        self.assertNotIn("0", chat.folds)
        self.assertNotIn(source, chat.archive.sources)
        chat.send("hello")
        self.assertNotIn("SUMMARY OF EARLIER CONVERSATION", self.prompt())

    def test_chats_are_kept(self):
        chat = self.chat()
        chat.send("remember me")
        chat.pin(2)
        chat.send("a reply", thread=2)
        store = ChatStore(self.h)                                       # a fresh start
        again = store.load(chat.id)
        self.assertEqual([(m.num, m.thread, m.pinned) for m in again.messages],
                         [(m.num, m.thread, m.pinned) for m in chat.messages])
        self.assertEqual(again.title, "remember me")
        self.assertEqual([c["id"] for c in store.list()], [chat.id])
        self.assertEqual(json.loads(chat.path.read_text())["title"], "remember me")
        store.delete(chat.id)
        self.assertEqual(store.list(), [])
        with self.assertRaises(NotFound):
            store.load(chat.id)
        with self.assertRaises(Invalid):
            store.load("../x")
