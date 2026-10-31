"""Chat mode: a conversation with the model, with its context kept small.

- Tools are off. The model gets no tool list, and its replies are never read as tool
  calls. A tool, or another mode, runs only when a message asks for it with a command
  line at its start (`/read src/app.py`); the rest of the message is the question.
  `/agent TASK` lends the model the tools for that one message, and only its answer
  stays in the conversation, not the calls it made.
- Each reply's context is built by the context manager: a short system prompt, the
  rolling summary of turns folded away when the window filled (archived, so /recall
  finds them), the pinned messages, and the recent turns.
- Pin a message to keep it in every later prompt, in the conversation and in its
  threads (CAG), even once it has been folded into the summary.
- Reply to a message to start a thread on it. A thread's context is that message (with
  the question it answered) and the thread's own replies, not the whole conversation.
- Rewind to a message to clear everything after it: the messages, the threads on them,
  their pins, and any summary folded since. Rewinding a thread clears only the thread.

A chat is kept as `.smallharness/chats/<id>.json`, its archive of folded turns beside it."""
from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import prompts
from .context import ContextManager, RagStore, Session
from .context.rag import search
from .context.session import Injected, Turn
from .errors import HarnessError, Invalid, NotFound

if TYPE_CHECKING:
    from .agent import Events, Harness

# name -> (usage, what it does). Every tool is also a command under its own name.
COMMANDS: dict[str, tuple[str, str]] = {
    "read": ("/read PATH [START] [LINES]", "show a workspace file (safe_read_file)"),
    "ls": ("/ls [PATTERN] [DIR]", "list workspace files (safe_list_files)"),
    "grep": ("/grep TEXT [GLOB] [DIR]", "search file contents (safe_search_files)"),
    "run": ("/run COMMAND", "run a shell command; asks first (risky_run_command)"),
    "write": ("/write PATH", "write the last answer's code block (or text) to a file; asks first"),
    "tool": ("/tool NAME ARGS", "any tool; ARGS as JSON, key=value pairs, or in order"),
    "tools": ("/tools", "the tools there are"),
    "agent": ("/agent TASK", "the model may use tools for this one message (risky ones ask)"),
    "ask": ("/ask QUESTION", "research: answered from the knowledge base, citing excerpts"),
    "find": ("/find WORDS", "the knowledge-base excerpts for WORDS, without the model"),
    "note": ("/note TEXT", "save a note (#tags optional)"),
    "notes": ("/notes [WORDS]", "find notes, or list the latest ones"),
    "plan": ("/plan GOAL", "draft a plan for GOAL"),
    "runplan": ("/runplan [ID]", "run a plan (default: the last one drafted here); risky steps ask"),
    "recall": ("/recall WORDS", "search this chat's turns that were folded into the summary"),
    "help": ("/help", "these commands"),
}
ALIASES = {"read": "safe_read_file", "ls": "safe_list_files", "grep": "safe_search_files",
           "run": "risky_run_command"}
_WHOLE_LINE = {"risky_run_command": "command"}     # the whole line is this one argument
_CODE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_ID = re.compile(r"[\w-]+")


@dataclass
class Message:
    num: int
    role: str                       # user | assistant | tool
    text: str                       # what the chat shows
    seen: str = ""                  # what the model sees of it; "" leaves it out (help, errors)
    thread: int = 0                 # the message whose thread it is in; 0 = the conversation
    kind: str = "chat"              # chat | tool | research | plan | note | recall | agent | info | error
    pinned: bool = False
    meta: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


@dataclass
class Fold:
    """One refresh of a line's context: its turns before `keep_from` went into `summary`,
    and into the archive as `source`. `last` was the line's last message then, so
    rewinding to before it undoes the fold."""
    last: int
    keep_from: int
    summary: str
    source: str


class ChatContext(ContextManager):
    hold = False      # True: no refresh (an agent turn's steps are not kept, so not folded)

    def needs_refresh(self, built=None) -> bool:
        return not self.hold and super().needs_refresh(built)


def parse(text: str) -> tuple[list[tuple[str, str]], str]:
    """The command lines at the start of a message, and the question after them.
    /agent takes the rest of the message as its task."""
    lines = text.strip().split("\n")
    commands: list[tuple[str, str]] = []
    i = 0
    while i < len(lines) and lines[i].startswith("/"):
        name, _, arg = lines[i][1:].partition(" ")
        name = name.strip().lower()
        if name == "agent":
            commands.append((name, "\n".join([arg, *lines[i + 1:]]).strip()))
            return commands, ""
        commands.append((name, arg.strip()))
        i += 1
    return commands, "\n".join(lines[i:]).strip()


def tool_args(tool, arg: str) -> dict:
    """A command line's arguments for `tool`: a JSON object, key=value pairs, or values
    in the order of its parameters."""
    arg = arg.strip()
    if not arg:
        return {}
    if tool.name in _WHOLE_LINE:
        return {_WHOLE_LINE[tool.name]: arg}
    if arg.startswith("{"):
        try:
            args = json.loads(arg)
        except json.JSONDecodeError as e:
            raise Invalid(f"the arguments are not valid JSON: {e}") from None
        if not isinstance(args, dict):
            raise Invalid("the JSON arguments must be an object")
        return args
    try:
        words = shlex.split(arg)
    except ValueError as e:
        raise Invalid(f"cannot read the arguments: {e}") from None
    if all("=" in w and w.split("=", 1)[0] in tool.params for w in words):
        return dict(w.split("=", 1) for w in words)
    names = list(tool.params)
    if len(words) > len(names):
        raise Invalid(f"too many arguments; usage: {tool.usage()}")
    return dict(zip(names, words))


class Chat:
    def __init__(self, h: Harness, path: Path, data: dict | None = None):
        data = data or {}
        self.h = h
        self.path = path
        self.id = path.stem
        self.title: str = data.get("title", "")
        self.created: float = data.get("created", time.time())
        self.updated: float = data.get("updated", self.created)
        self.messages = [Message(**m) for m in data.get("messages", [])]
        self.folds = {k: [Fold(**f) for f in v] for k, v in data.get("folds", {}).items()}
        self._archive: RagStore | None = None
        self._research = None

    # ------------------------------------------------------------ storage
    def save(self) -> None:
        self.updated = time.time()
        data = {"title": self.title, "created": self.created, "updated": self.updated,
                "messages": [asdict(m) for m in self.messages],
                "folds": {k: [asdict(f) for f in v] for k, v in self.folds.items()}}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def archive(self) -> RagStore:
        """The turns folded into a summary, searchable (/recall)."""
        if self._archive is None:
            self._archive = RagStore(self.path.with_suffix(".sqlite"), self.h.embedder)
        return self._archive

    @property
    def research(self):
        if self._research is None:
            from .research import Research
            self._research = Research(self.h, f"chat-{self.id}")
        return self._research

    def close(self) -> None:
        if self._archive is not None:
            self._archive.close()
            self._archive = None

    def to_dict(self) -> dict:
        threads: dict[int, int] = {}
        for m in self.messages:
            if m.thread:
                threads[m.thread] = threads.get(m.thread, 0) + 1
        return {"id": self.id, "title": self.title or "New chat", "created": self.created,
                "updated": self.updated, "messages": [asdict(m) for m in self.messages],
                "threads": threads, "pinned": [m.num for m in self.messages if m.pinned],
                "summaries": {k: v[-1].summary for k, v in self.folds.items() if v}}

    # ----------------------------------------------------------- messages
    def get(self, num: int) -> Message:
        for m in self.messages:
            if m.num == num:
                return m
        raise NotFound(f"no message #{num}")

    def line(self, thread: int = 0) -> list[Message]:
        """The conversation (0), or one thread's replies."""
        return [m for m in self.messages if m.thread == thread]

    def thread_of(self, num: int) -> int:
        """Replying to a message opens its thread; a reply inside a thread stays in it."""
        m = self.get(num)
        return m.thread or m.num

    def _add(self, role: str, text: str, thread: int, seen: str | None = None, kind: str = "chat",
             **meta) -> Message:
        num = max((m.num for m in self.messages), default=0) + 1
        m = Message(num, role, text, text if seen is None else seen, thread, kind, meta=meta)
        self.messages.append(m)
        return m

    def pin(self, num: int, on: bool = True) -> Message:
        m = self.get(num)
        if on and not m.seen:
            raise Invalid(f"#{num} is not part of the context, so it cannot be pinned")
        m.pinned = on
        self.save()
        return m

    def rewind(self, to: int, thread: int = 0) -> int:
        """Clear everything after message `to` (0: the whole conversation); in a thread,
        only the thread's replies after it. Returns how many messages went."""
        if thread:
            if thread != to and self.get(to).thread != thread:
                raise Invalid(f"#{to} is not in the thread on #{thread}")
            gone = {m.num for m in self.messages if m.thread == thread and m.num > to}
            lines = [str(thread)]
        else:
            if to and self.get(to).thread:
                raise Invalid(f"#{to} is in a thread: rewind it from the thread")
            gone = {m.num for m in self.messages if m.num > to}
            lines = list(self.folds)
        self.messages = [m for m in self.messages if m.num not in gone]
        for key in lines:
            kept = []
            for f in self.folds.get(key, []):
                if f.last > to or int(key) in gone:
                    self.archive.remove(f.source)
                else:
                    kept.append(f)
            if kept:
                self.folds[key] = kept
            else:
                self.folds.pop(key, None)
        self.save()
        return len(gone)

    # ------------------------------------------------------------ context
    def _turns(self, thread: int) -> list[Turn]:
        msgs = []
        if thread:                       # a thread starts from its message, and the question it answered
            root = self.get(thread)
            if root.role == "assistant":
                asked = [m for m in self.line(root.thread) if m.num < root.num and m.role == "user" and m.seen]
                if asked:
                    msgs.append(asked[-1])
            msgs.append(root)
        msgs += self.line(thread)
        return [Turn(m.num, m.role, m.seen) for m in msgs if m.seen]

    def context(self, thread: int = 0) -> ChatContext:
        """The context manager for a reply in the conversation or a thread: its turns,
        the summary folded so far, and the pinned messages it does not already show."""
        session = Session(None, f"chat-{self.id}-{thread}")
        turns = self._turns(thread)
        session.history = list(turns)
        folds = self.folds.get(str(thread), [])
        if folds:
            session.summary = folds[-1].summary
            session.window = [t for t in turns if t.n >= folds[-1].keep_from]
            session.refreshes = len(folds)
        else:
            session.window = list(turns)
        shown = {t.n for t in session.window}
        session.injected = [Injected(f"pinned #{m.num}", m.seen)
                            for m in self.messages if m.pinned and m.seen and m.num not in shown]
        tools = self.h.tools

        def system(with_tools: bool) -> str:
            return prompts.CHAT_AGENT_SYSTEM + "\n\n" + tools.prompt_block() if with_tools else prompts.CHAT_SYSTEM
        return ChatContext(self.h.cfg, self.h.counter, session, self.h.project_rag, self.archive,
                           system, retrieve=False)

    def _fold(self, ctx: ChatContext, thread: int, built, events: Events) -> bool:
        """Refresh the context if it is filling up, and remember the fold."""
        if not ctx.needs_refresh(built):
            return False
        before = [t.n for t in ctx.session.window]
        moved = ctx.refresh(self.h.summarize)
        if not moved:
            return False
        old = before[:moved]
        last = max([thread, *(m.num for m in self.line(thread))])
        keep_from = ctx.session.window[0].n if ctx.session.window else before[-1] + 1
        self.folds.setdefault(str(thread), []).append(
            Fold(last, keep_from, ctx.session.summary, f"session:{ctx.session.id}#{old[0]}-{old[-1]}"))
        events.on_refresh(moved, built)
        return True

    def _extra(self, thread: int) -> str:
        return prompts.CHAT_THREAD.format(num=thread) if thread else ""

    def build(self, thread: int = 0, events: Events | None = None):
        """The prompt for the next reply (folding first if the window is filling up)."""
        ctx = self.context(thread)
        built = ctx.build(None, self._extra(thread), tools=False)
        if self._fold(ctx, thread, built, events or self.h.events):
            built = ctx.build(None, self._extra(thread), tools=False)
        return built

    # ---------------------------------------------------------- sending
    def check(self, text: str) -> tuple[list[tuple[str, str]], str]:
        """A message's commands and question; refused if it is empty or names an
        unknown command, before anything is kept."""
        if not text.strip():
            raise Invalid("write a message")
        commands, question = parse(text)
        for name, _ in commands:
            if name not in COMMANDS and not self.h.tools.get(name):
                raise Invalid(f"unknown command /{name}; /help lists them")
        return commands, question

    def send(self, text: str, thread: int = 0, events: Events | None = None,
             on_message: Callable[[Message], None] | None = None) -> list[Message]:
        """A message, its commands, and the model's reply. `thread`: reply in the thread
        on that message. Returns the messages added, each also passed to `on_message`."""
        commands, question = self.check(text)
        text = text.strip()
        thread = self.thread_of(thread) if thread else 0
        added: list[Message] = []

        def keep(m: Message) -> None:
            added.append(m)
            self.save()
            if on_message:
                on_message(m)
        if not self.title:
            self.title = text.splitlines()[0][:60]
        user = self._add("user", text, thread, seen=question,
                         **({"commands": [n for n, _ in commands]} if commands else {}))
        keep(user)
        for name, arg in commands:
            try:
                results = self._command(name, arg, thread, user, events)
            except (HarnessError, ValueError) as e:
                keep(self._add("tool", str(e), thread, seen="", kind="error", **{"for": user.num}))
                return added
            for m in results:
                keep(m)
        if question:
            keep(self._reply(thread, events))
        return added

    def _reply(self, thread: int, events: Events | None) -> Message:
        built = self.build(thread, events)
        text = self.h.generate(built.prompt, stream=events is not None, events=events).strip()
        return self._add("assistant", text, thread,
                         context={"tokens": built.total, "budget": built.budget, "sections": built.sections})

    # --------------------------------------------------------- commands
    def _command(self, name: str, arg: str, thread: int, user: Message, events: Events | None) -> list[Message]:
        def add(role: str, text: str, seen: str | None = None, kind: str = "tool", **meta) -> Message:
            return self._add(role, text, thread, seen, kind, **{"for": user.num, **meta})

        def need(what: str) -> str:
            if not arg:
                raise Invalid(f"usage: {COMMANDS[name][0]}  ({what})")
            return arg
        if name in ALIASES or self.h.tools.get(name):
            tool = self.h.tools.get(ALIASES.get(name, name))
            return [self._tool(tool.name, tool_args(tool, arg), add, events)]
        if name == "tool":
            tname, _, rest = need("a tool name").partition(" ")
            tool = self.h.tools.get(tname)
            if not tool:
                raise Invalid(f"no tool named {tname!r}; /tools lists them")
            return [self._tool(tool.name, tool_args(tool, rest), add, events)]
        if name == "write":
            path = need("the file to write")
            source = [m for m in self.line(thread) if m.role == "assistant"]
            if not source:
                raise Invalid("there is no answer to write yet")
            blocks = _CODE.findall(source[-1].text)
            content = blocks[-1] if blocks else source[-1].text + "\n"
            return [self._tool("risky_write_file", {"path": path, "content": content}, add, events)]
        if name in ("tools", "help"):
            return [add("tool", self._listing(name), seen="", kind="info")]
        if name == "agent":
            return [self._agent(need("what to do"), thread, user, events)]
        if name == "ask":
            r = self.research
            answer = r.ask(need("a question"), events)
            refs = [r.refs[n - 1] for n in answer.refs]
            cites = ", ".join(f"[{x.num}] {x.cite()}" for x in refs)
            return [add("assistant", answer.text, kind="research",
                        seen=f"(research answer to: {answer.question})\n{answer.text}" + (f"\n(sources: {cites})" if cites else ""),
                        refs=[{"num": x.num, "source": x.source, "line": x.start_line, "text": x.text} for x in refs])]
        if name == "find":
            refs = self.research.find(need("what to look for"))
            text = "\n\n".join(f"[{x.num}] {x.cite()}\n{x.text.strip()}" for x in refs) or "nothing matches"
            return [add("tool", text, seen=f"(knowledge-base excerpts for: {arg})\n{text}", kind="research")]
        if name == "note":
            note = self.h.notes.add(need("the note"))
            return [add("tool", f"saved note {note.id}: {note.title}", kind="note")]
        if name == "notes":
            notes = [n for n, _ in self.h.notes.search(arg)] if arg else self.h.notes.all()[:10]
            text = "\n".join(f"[{n.id}] {n.title}" + (f"  #{' #'.join(n.tags)}" if n.tags else "") for n in notes)
            body = "\n\n".join(f"[{n.id}] {n.text.strip()[:400]}" for n in notes)
            return [add("tool", text or "no notes match", seen=f"(notes{': ' + arg if arg else ''})\n{body or 'none'}",
                        kind="note")]
        if name == "plan":
            from .planning import create_plan
            goal = need("the goal")
            plan, warnings = create_plan(self.h, goal)
            return [add("assistant", plan.render(), seen=f"(plan for: {goal})\n{plan.render()}", kind="plan",
                        plan=plan.id, warnings=warnings)]
        if name == "runplan":
            return [self._run_plan(arg, add, events)]
        if name == "recall":
            hits = search([self.archive], need("what to look for"), 3)
            text = "\n\n".join(f"[{c.source.rsplit('#', 1)[-1]}]\n{c.text.strip()}" for c in hits) \
                or "nothing in the folded turns matches"
            return [add("tool", text, seen=f"(earlier in this chat, about: {arg})\n{text}", kind="recall")]
        raise Invalid(f"unknown command /{name}")

    def _tool(self, name: str, args: dict, add, events: Events | None) -> Message:
        ctx = self.h.tool_context()
        ctx.extra["origin"] = f"chat:{self.id}"           # files it writes are relics
        outcome = self.h.run_tool(name, args, ctx, events)
        shown = {k: v for k, v in args.items() if k != "content"}
        label = " ".join(f"{k}={v}" for k, v in shown.items())
        ok = outcome.ok and not outcome.text.startswith("ERROR")     # tools report some failures as text
        return add("tool", outcome.text, seen=f"({name} {label}): {outcome.text}",
                   tool=name, args=shown, ok=ok, denied=outcome.denied)

    def _listing(self, which: str) -> str:
        if which == "tools":
            return "\n".join(f"{t.signature()} - {t.description.splitlines()[0]}" for t in self.h.tools)
        rows = [f"{usage}  - {about}" for usage, about in COMMANDS.values()]
        rows.append("/TOOL_NAME ARGS  - any tool by its own name: " + ", ".join(t.name for t in self.h.tools))
        return ("Commands go on their own lines at the start of a message; the rest is your question.\n"
                + "\n".join(rows))

    def _agent(self, task: str, thread: int, user: Message, events: Events | None) -> Message:
        """The model, with tools, for this one task. Only the answer is kept in the
        conversation; the calls and their results are kept with it, for showing."""
        from .agent import Agent
        ctx = self.context(thread)
        built = ctx.build(task, self._extra(thread), tools=True)
        self._fold(ctx, thread, built, events or self.h.events)
        ctx.hold = True
        agent = Agent(self.h, ctx, events or self.h.events)
        agent.tool_ctx.extra["origin"] = f"chat:{self.id}"
        start = len(ctx.session.history)
        answer = agent.respond(task, auto_tools=True, extra_system=self._extra(thread), stream=events is not None)
        steps = [{"role": t.role, "tool": t.meta.get("tool", ""), "text": t.content, "ok": t.meta.get("ok", True)}
                 for t in ctx.session.history[start + 1:-1]]
        user.seen = task                                   # later turns see what was asked
        return self._add("assistant", answer, thread, kind="agent", steps=steps, **{"for": user.num})

    def _run_plan(self, plan_id: str, add, events: Events | None) -> Message:
        from .planning import PlanRunner
        if not plan_id:
            drafted = [m.meta["plan"] for m in self.messages if m.meta.get("plan")]
            if not drafted and not self.h.plans.latest():
                raise Invalid("no plan yet: /plan GOAL drafts one")
            plan_id = drafted[-1] if drafted else self.h.plans.latest().id
        plan = self.h.plans.load(plan_id)
        ev = events or self.h.events
        PlanRunner(self.h, plan, "fresh", ev).run(
            on_start=lambda t: ev.on_notice(f"step {t.num}: {t.task}"),
            on_end=lambda t: ev.on_notice(f"step {t.num} {t.status}: {t.result}"),
            should_continue=lambda t: False)
        text = plan.render(results=True)
        return add("tool", text, seen=f"(ran plan {plan.id})\n{text}", kind="plan", plan=plan.id,
                   finished=plan.finished)


class ChatStore:
    """The workspace's chats, each opened once and kept while the harness runs."""

    def __init__(self, h: Harness):
        self.h = h
        self._open: dict[str, Chat] = {}

    @property
    def dir(self) -> Path:
        return self.h.cfg.state.chats

    def _path(self, chat_id: str) -> Path:
        if not _ID.fullmatch(chat_id or ""):
            raise Invalid(f"bad chat id {chat_id!r}")
        return self.dir / f"{chat_id}.json"

    def new(self, save: bool = True) -> Chat:
        """A new chat; with save=False it is kept only once it has a message."""
        chat_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        chat = self._open[chat_id] = Chat(self.h, self._path(chat_id))
        if save:
            chat.save()
        return chat

    def load(self, chat_id: str) -> Chat:
        if chat_id in self._open:
            return self._open[chat_id]
        path = self._path(chat_id)
        if not path.is_file():
            raise NotFound(f"no chat {chat_id}")
        chat = self._open[chat_id] = Chat(self.h, path, json.loads(path.read_text(encoding="utf-8")))
        return chat

    def list(self) -> list[dict]:
        out = []
        for p in self.dir.glob("*.json"):
            chat = self._open.get(p.stem)
            if chat is None:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                title, updated, count = data.get("title", ""), data.get("updated", 0), len(data.get("messages", []))
            else:
                title, updated, count = chat.title, chat.updated, len(chat.messages)
            out.append({"id": p.stem, "title": title or "New chat", "updated": updated, "messages": count})
        return sorted(out, key=lambda c: c["updated"], reverse=True)

    def latest(self) -> Chat | None:
        chats = self.list()
        return self.load(chats[0]["id"]) if chats else None

    def delete(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if not path.is_file():
            raise NotFound(f"no chat {chat_id}")
        chat = self._open.pop(chat_id, None)
        if chat:
            chat.close()
        db = path.with_suffix(".sqlite")
        for f in (path, db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            f.unlink(missing_ok=True)

    def close(self) -> None:
        for chat in self._open.values():
            chat.close()
