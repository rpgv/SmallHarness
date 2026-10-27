"""Chat mode in the terminal: a conversation, with tools only on command."""
from __future__ import annotations

import time

from .. import ui
from ..chat import COMMANDS, Chat, Message
from .shell import Shell

HELP = [
    ("MESSAGE", "talk with the model; it has no tools unless a command asks for one"),
    ("/read PATH  (then a question)", "commands start the message, one per line; /commands lists them all"),
    ("/agent TASK", "let the model use tools for this one message (risky ones ask)"),
    ("/reply N TEXT", "answer message N in a thread: the thread's context, not the whole chat"),
    ("/thread N, /main", "enter the thread on message N, so messages go there; go back"),
    ("/pin N, /unpin N", "keep message N in every later prompt (CAG), or stop"),
    ("/rewind N", "clear everything after message N (in a thread: only the thread's replies)"),
    ("/history", "the conversation so far, or the thread you are in"),
    ("/chats, /open ID, /new", "your chats: list, open one, start another"),
    ("/title TEXT, /forget ID", "rename this chat; delete a chat"),
    ("/commands", "the commands a message can start with"),
]


class ChatShell(Shell):
    name = "chat"
    about = "talk with the model; tools and other modes only when a message asks for them"
    hint = "type a message"
    help = HELP

    def __init__(self, h, events, no_color: bool = False):
        super().__init__(h, events, no_color)
        self.chat: Chat = h.chats.new(save=False)
        self.thread = 0

    def prompt_name(self) -> str:
        return f"chat #{self.thread}" if self.thread else "chat"

    def banner(self) -> None:
        super().banner()
        n = len(self.h.chats.list())
        if n:
            print(ui.dim(f"  a new chat · {n} earlier one(s): /chats, /open ID"))

    # ----------------------------------------------------------- sending
    def command(self, line: str):
        name = line[1:].partition(" ")[0].partition("\n")[0].strip().lower()
        if hasattr(self, f"cmd_{name.replace('-', '_')}"):
            return super().command(line)
        if name in COMMANDS or self.h.tools.get(name):
            return self.text(line)
        return super().command(line)          # says it is unknown

    def text(self, line: str):
        self.send(line, self.thread)

    def send(self, text: str, thread: int) -> None:
        self.ev.printed = False
        with self.busy("thinking"):
            self.chat.send(text, thread, self.ev, on_message=self.show_new)

    def show_new(self, m: Message) -> None:
        """A message as it arrives. Streamed answers and tool calls are printed already."""
        self.ev.spinner.stop()
        self.ev.on_text_end()
        if m.role == "user":
            return
        if m.role == "assistant" and not self.ev.printed:
            print(ui.accent("◆ ") + m.text)
        elif m.role == "tool" and m.kind != "tool":           # not through a tool call: not shown yet
            print(ui.err(m.text) if m.kind == "error" else m.text)
        used = [s["tool"] for s in m.meta.get("steps", []) if s["role"] == "assistant" and s["tool"]]
        if used:
            print(ui.dim(f"  used {', '.join(used)} (kept with the answer, not in the context)"))
        if m.meta.get("refs"):
            print(ui.dim("  refs " + " ".join(f"[{r['num']}] {r['source']}:{r['line']}" for r in m.meta["refs"])))
        if m.seen:
            where = f"/reply {m.num}" if not self.thread else f"thread on #{self.thread}"
            print(ui.dim(f"  #{m.num} · {where} · /pin {m.num} · /rewind {m.num}"))
        self.ev.printed = False
        self.ev.spinner.start("thinking")

    # ----------------------------------------------------------- threads
    def cmd_reply(self, arg):
        num, _, text = arg.partition(" ")
        if not num.lstrip("#").isdigit() or not text.strip():
            print(ui.err("usage: /reply N TEXT"))
            return
        self.send(text, int(num.lstrip("#")))

    def cmd_thread(self, arg):
        if not arg.lstrip("#").isdigit():
            print(ui.err("usage: /thread N   (the message to open the thread on)"))
            return
        self.thread = self.chat.thread_of(int(arg.lstrip("#")))
        print(ui.dim(f"  in the thread on #{self.thread}: messages go here, with its context only · /main to leave"))
        self.cmd_history("")

    def cmd_main(self, _):
        self.thread = 0
        print(ui.dim("  back in the conversation"))

    # -------------------------------------------------------- pins, rewind
    def cmd_pin(self, arg):
        for n in _numbers(arg):
            self.chat.pin(n)
            print(ui.ok(f"  ✓ #{n} pinned: every later prompt keeps it, threads too"))

    def cmd_unpin(self, arg):
        for n in _numbers(arg):
            self.chat.pin(n, False)
            print(ui.ok(f"  ✓ #{n} unpinned"))

    def cmd_rewind(self, arg):
        if not arg.lstrip("#").isdigit():
            print(ui.err("usage: /rewind N   (0: the whole conversation)"))
            return
        to = int(arg.lstrip("#"))
        where = f"the thread on #{self.thread}" if self.thread else "the conversation"
        if not ui.confirm(f"Clear everything in {where} after #{to}?", tty_only=False):
            return
        gone = self.chat.rewind(to, self.thread)
        print(ui.ok(f"  ✓ {gone} message(s) cleared; the context is back to #{to}"))

    # ------------------------------------------------------------- chats
    def cmd_history(self, _):
        msgs = self.chat.line(self.thread)
        if self.thread:
            msgs = [self.chat.get(self.thread), *msgs]
        summary = self.chat.to_dict()["summaries"].get(str(self.thread))
        if summary:
            print(ui.dim("  earlier turns, folded into the summary:\n    " + summary.replace("\n", "\n    ")))
        if not msgs:
            print(ui.dim("  nothing yet"))
        threads = self.chat.to_dict()["threads"]
        for m in msgs:
            who = {"user": ui.sky("you"), "assistant": ui.accent("model"), "tool": ui.steel(m.meta.get("tool") or m.kind)}[m.role]
            pin = ui.accent(" ◆pinned") if m.pinned else ""
            replies = ui.dim(f"  ({threads[m.num]} in thread)") if m.num in threads and not self.thread else ""
            first = " ".join(m.text.split())
            print(f"  {ui.dim(f'#{m.num}')} {who}{pin} {first[:150]}{'…' if len(first) > 150 else ''}{replies}")

    def cmd_chats(self, _):
        chats = self.h.chats.list()
        if not chats:
            print(ui.dim("  no chats yet"))
        for c in chats:
            mark = ui.accent("◆") if c["id"] == self.chat.id else " "
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["updated"]))
            print(f"  {mark} {ui.sky(c['id'])}  {c['title']}  " + ui.dim(f"{c['messages']} message(s) · {when}"))

    def cmd_open(self, arg):
        if not arg:
            print(ui.err("usage: /open ID   (see /chats)"))
            return
        self.chat, self.thread = self.h.chats.load(arg), 0
        print(ui.ok(f"  ✓ opened {self.chat.title or self.chat.id}"))
        self.cmd_history("")

    def cmd_new(self, _):
        self.chat, self.thread = self.h.chats.new(save=False), 0
        print(ui.ok("  ✓ a new chat"))

    def cmd_title(self, arg):
        if not arg:
            print(ui.dim(f"  {self.chat.title or '(no title yet)'}"))
            return
        self.chat.title = arg
        self.chat.save()
        print(ui.ok("  ✓ renamed"))

    def cmd_forget(self, arg):
        if not arg or not ui.confirm(f"Delete the chat {arg}?", tty_only=False):
            return
        self.h.chats.delete(arg)
        if arg == self.chat.id:
            self.cmd_new("")
        print(ui.ok(f"  ✓ deleted {arg}"))

    def cmd_commands(self, _):
        rows = [v for k, v in COMMANDS.items() if k != "help"]      # /help here is the terminal's
        w = max(len(u) for u, _ in rows)
        for usage, about in rows:
            print(f"  {ui.sky(usage.ljust(w))}  {ui.dim(about)}")
        print(ui.dim("  and any tool by its own name: " + ", ".join(t.name for t in self.h.tools)))
        print(ui.dim("  commands go on their own lines at the start of a message (\\ continues a line);\n"
                     "  the rest is your question about what they found"))


def _numbers(arg: str) -> list[int]:
    nums = [int(w.lstrip("#")) for w in arg.replace(",", " ").split() if w.lstrip("#").isdigit()]
    if not nums:
        raise ValueError("give a message number, e.g. /pin 4")
    return nums
