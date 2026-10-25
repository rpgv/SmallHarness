"""The harness core (wires backend pool, context, tools) and the agent turn loop."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from . import prompts
from .backends import Backend, make_backend
from .config import Config, changed_keys
from .context import ContextManager, RagStore, Session
from .context.manager import Built, extractive_summary
from .engines import make_embedder_for
from .notes import NoteStore
from .plans import PlanStore
from .pool import ModelPool
from .tokens import TokenCounter
from .toolcall import ToolCallError, parse_tool_call
from .tools import Tool, ToolContext, ToolOutcome, ToolRegistry

MAX_PARSE_FAILURES = 2

# Settings that change which model runs or how it is loaded: changing one restarts the model.
_RESTART_KEYS = ("model.backend", "model.path", "model.name", "model.context_tokens",
                 "model.auto_context_max", "litert.", "ollama.")


class Events:
    """UI hooks. The default denies every type-1 tool (safe for headless use)."""

    def on_text(self, chunk: str) -> None: ...
    def on_text_end(self) -> None: ...
    def on_tool_call(self, name: str, args: dict) -> None: ...
    def on_tool_result(self, outcome: ToolOutcome) -> None: ...
    def on_refresh(self, moved: int, before: Built) -> None: ...
    def on_notice(self, msg: str) -> None: ...
    def on_stats(self, stats: dict) -> None: ...   # timing of one streamed model call

    def permit(self, tool: Tool, args: dict) -> bool:
        return False


class ForwardingEvents(Events):
    """Passes every event on to `inner`, except the kinds named in `drop`. Events added
    to Events later pass through on their own; a wrapper that changes one overrides it."""

    def __init__(self, inner: Events, drop: tuple[str, ...] = ()):
        self.inner = inner
        self.drop = set(drop)

    def permit(self, tool: Tool, args: dict) -> bool:
        return self.inner.permit(tool, args)


def _forward(name: str):
    def method(self, *args):
        if name not in self.drop:
            return getattr(self.inner, name)(*args)
    method.__name__ = name
    return method


for _name in [n for n in vars(Events) if n.startswith("on_")]:
    setattr(ForwardingEvents, _name, _forward(_name))


class Harness:
    def __init__(self, cfg: Config, events: Events | None = None,
                 backend_factory: Callable[[], Backend] | None = None):
        self.cfg = cfg
        self.events = events or Events()
        state = cfg.state
        self.factory = backend_factory or (lambda: make_backend(cfg))
        self.pool = ModelPool(cfg, self.factory)
        self.counter = TokenCounter()
        self.tools = self._load_tools()
        self.embedder = make_embedder_for(cfg, self._model_server_url)
        self.project_rag = RagStore(state.project_rag, self.embedder)
        self.notes = NoteStore.for_workspace(state, self.project_rag)
        self.plans = PlanStore(state.plans)
        self.auto_tools = cfg["tools"]["auto"]
        self._permit_lock = threading.Lock()
        self.open_session(Session(state.sessions))
        state.prune_session_indexes(int(cfg["rag"].get("session_index_days", 30)), self.session.id)

    def _load_tools(self) -> ToolRegistry:
        dirs = [Path(d).expanduser() for d in self.cfg["tools"]["extra_dirs"]]
        # The workspace's own tools are code from the project: imported only if it is trusted.
        trusted = self.cfg.workspace_trusted()
        if trusted:
            dirs.insert(0, self.cfg.state.tools)
        registry = ToolRegistry(dirs).load()
        if not trusted and self.cfg.workspace_code().tools:
            registry.warnings.append("workspace tools in .smallharness/tools/ not loaded: "
                                     "the workspace is not trusted")
        return registry

    # ------------------------------------------------------------- settings
    def reconfigure(self, data: dict) -> list[str]:
        """Apply new settings while running. The model is restarted only if a setting
        that selects or loads it changed; if the new model cannot start, the old
        settings and model are restored and the error is raised.
        Returns a line per change applied."""
        changed = changed_keys(self.cfg.data, data)
        if not changed:
            return []
        if self.pool.busy:
            raise RuntimeError("the model is busy; change settings when it is idle")
        old = self.cfg.data
        self.cfg.data = data
        done = []
        if any(k.startswith(_RESTART_KEYS) for k in changed) and not self.pool.instances:
            # Nothing loaded yet (the web interface loads on first use): just use the new settings.
            self.pool.close()
            self.pool = ModelPool(self.cfg, self.factory)
            self.counter.reset()
            done.append(f"model set: {self.cfg.model_name}, {self.cfg.context_tokens} ctx (loads when first used)")
        elif any(k.startswith(_RESTART_KEYS) for k in changed):
            try:
                self.restart_model()
            except Exception as e:
                self.cfg.data = old
                try:
                    self.restart_model()
                    kept = "kept the previous model"
                except Exception as e2:
                    kept = f"the previous model did not restart either ({e2}); no model is loaded"
                raise RuntimeError(f"new model could not start: {e}\n{kept}") from None
            done.append(f"model restarted: {self.cfg.model_name}, {self.cfg.context_tokens} ctx")
        elif any(k.startswith("resources.") for k in changed):
            stopped = self.pool.shrink()
            if stopped:
                done.append(f"stopped {stopped} instance(s) above the new limit")
        if any(k.startswith("rag.embed.") for k in changed):
            self.embedder = make_embedder_for(self.cfg, self._model_server_url)
            for store in (self.project_rag, self.session_rag):
                store.set_embedder(self.embedder)
            done.append(f"search: {self.search_mode()}"
                        + (" (run /index again to embed files already indexed)" if self.embedder else ""))
        if "tools.extra_dirs" in changed:
            self.tools = self._load_tools()
            done.append(f"tools reloaded: {len(list(self.tools))}")
        if "tools.auto" in changed:
            self.auto_tools = self.cfg["tools"]["auto"]
        # A smaller window may no longer hold the conversation.
        built = self.ctx.build()
        if built.total > built.budget or self.ctx.needs_refresh(built):
            moved = self.ctx.refresh(self.summarize)
            if moved:
                done.append(f"context refreshed for the new window: {moved} turn(s) moved to the summary")
        return done

    def _model_server_url(self) -> str:
        """For [rag.embed].backend = "server" without a base_url: the model's own server
        (Ollama also serves /v1/embeddings)."""
        from .engines import engine
        url = engine(self.cfg).server_url()
        if url:
            return url
        be = self.pool.instances[0] if self.pool.instances else None
        return getattr(be, "base", "") if getattr(be, "mode", "") == "server" else ""

    def backend_mode(self) -> str:
        be = self.pool.instances[0] if self.pool.instances else None
        return getattr(be, "mode", "") or ""

    def search_mode(self) -> str:
        if not self.embedder:
            return "keywords (BM25)"
        state = f"hybrid: keywords + {self.embedder.id}"
        err = self.project_rag.embed_error or self.session_rag.embed_error
        return state + (f" — embeddings failing: {err}" if err else "")

    def _import_for_server(self) -> None:
        """Server mode serves models from LiteRT-LM's own store, not from files: import
        the model file there first ([models].auto_import)."""
        from .backends import server_model_id
        from .backends.litert import resolve_mode
        from .models import ensure_imported, server_id
        cfg = self.cfg
        path = cfg["model"]["path"]
        if (cfg["model"]["backend"] != "litert" or not cfg["models"].get("auto_import")
                or not path or not Path(path).is_file()):
            return
        try:
            if resolve_mode(cfg["litert"]) != "server":
                return
        except RuntimeError:
            return  # no runtime configured: starting the model reports it
        model_id = server_model_id(cfg)
        if model_id != server_id(path):
            return  # the server was told to serve another model; the file is for the RAM estimate
        srv = cfg["litert"]["server"]
        cmd0 = srv["command"][0] if srv.get("launch") and srv.get("command") else ""
        exe = cmd0 if Path(cmd0).name.startswith("litert-lm") else "litert-lm"  # same install as serve
        if ensure_imported(path, model_id, exe):
            self.events.on_notice(f"imported {Path(path).name} into the LiteRT-LM store as {model_id!r}")

    def restart_model(self) -> None:
        """Stop every instance and start one with the current settings."""
        self.counter.backend = None
        self.counter.reset()
        self.pool.close()
        self.pool = ModelPool(self.cfg, self.factory)
        self.start()

    # ------------------------------------------------------------- sessions
    def open_session(self, session: Session) -> None:
        old = getattr(self, "session_rag", None)
        self.session = session
        if old is not None:
            old.close()
        self.session_rag = RagStore(self.cfg.state.session_rag(session.id), self.embedder)
        self.ctx = self.new_context(session, self.session_rag)

    def new_context(self, session: Session, session_rag: RagStore, retrieve: bool = True) -> ContextManager:
        return ContextManager(self.cfg, self.counter, session, self.project_rag, session_rag,
                              self.system_prompt, retrieve)

    def tool_context(self, ctx: ContextManager | None = None) -> ToolContext:
        ctx = ctx or self.ctx
        return ToolContext(self.cfg.workspace, self.cfg, self.project_rag, ctx.session_rag,
                           ctx.session, notes=self.notes)

    def system_prompt(self, tools: bool = True) -> str:
        """For plan runs, the one place the model gets tools."""
        return prompts.RUN_SYSTEM + ("\n\n" + self.tools.prompt_block() if tools else "")

    # --------------------------------------------------------------- model
    def start(self) -> None:
        """Load the first instance and switch to its tokenizer if it has one."""
        self._import_for_server()
        self.pool.ensure_one()
        self.counter.backend = self.pool.instances[0]

    def generate_clean(self, prompt: str, system: str = prompts.RULES, history: list[tuple[str, str]] = (),
                       stream: bool = False, events: Events | None = None) -> str:
        """One model call with a clean context: only `system`, any `history` turns of this
        same exchange, and `prompt`. No conversation, retrieval or tool list."""
        turns = [*history, ("user", prompt)]
        return self.generate(prompts.render(self.cfg["model"]["template"], system, turns), stream, events)

    def generate(self, prompt: str, stream: bool = False, events: Events | None = None) -> str:
        events = events or self.events
        m = self.cfg["model"]
        stop = prompts.STOP.get(m["template"])
        with self.pool.acquire() as be:
            try:
                return self._generate(be, prompt, stream, events, stop, m["max_output_tokens"])
            finally:
                real = getattr(be, "last_prompt_tokens", None)
                if real:
                    be.last_prompt_tokens = None
                    # The window is one user message: the chat template adds a few tokens.
                    self.counter.observe(prompt, real)

    def _generate(self, be, prompt, stream, events, stop, max_tokens) -> str:
        if not stream:
            return be.generate(prompt, max_tokens, stop)
        text, streaming = "", None
        start, first, pieces = time.perf_counter(), None, 0
        for piece in be.stream(prompt, max_tokens, stop):
            if first is None and piece:
                first = time.perf_counter()
            pieces += 1
            text += piece
            if streaming is None:
                head = text.lstrip()
                if len(head) < 8 and "<tool>".startswith(head[:6]):
                    continue  # too early to tell if this is a tool call
                streaming = not head.startswith(("<tool", "```tool", "```json"))
                if streaming:
                    events.on_text(text)
                continue
            if streaming:
                events.on_text(piece)
        if streaming is None and text.strip() and not text.lstrip().startswith("<tool"):
            events.on_text(text)
            streaming = True
        if streaming:
            events.on_text_end()
        events.on_stats(self._stats(text, start, first, pieces))
        return text

    def _stats(self, text: str, start: float, first: float | None, pieces: int) -> dict:
        """Speed of one call. Tokens are estimated unless the backend can count them.
        Generation speed is measured from the first piece, so it leaves out the time to
        read the prompt; a backend that returns the reply in one piece cannot separate them."""
        end = time.perf_counter()
        first = first or end
        real = self.counter.backend.count_tokens(text) if self.counter.backend is not None else None
        tokens = real if real is not None else self.counter(text)
        streamed = pieces > 1 and end - first > 0.001
        span = end - first if streamed else end - start
        return {"tokens": tokens, "estimated": real is None, "first_s": round(first - start, 3),
                "total_s": round(end - start, 3), "streamed": streamed,
                "tokens_per_s": round(tokens / span, 1) if span > 0 else 0.0}

    def summarize(self, previous: str, text: str) -> str:
        """Fold `text` into the rolling summary, in pieces that fit the window."""
        piece_budget = self.ctx.budget // 2
        pieces, buf = [], ""
        for line in text.splitlines(keepends=True):
            if buf and self.counter(buf + line) > piece_budget:
                pieces.append(buf)
                buf = ""
            buf += line
        if buf:
            pieces.append(buf)
        summary = previous
        try:
            for piece in pieces:
                piece = self.counter.truncate(piece, piece_budget)
                prompt = prompts.SUMMARIZE.format(bullets=8, previous=summary or "(none)", transcript=piece)
                summary = self.generate(prompt).strip() or summary
        except Exception as e:
            self.events.on_notice(f"summary by model failed ({e}); using extractive summary")
            from .context.session import Turn
            return extractive_summary(previous, [Turn(0, "note", text)])
        return summary

    # ---------------------------------------------------------------- tools
    def run_tool(self, name: str, args: dict, tool_ctx: ToolContext | None = None,
                 events: Events | None = None) -> ToolOutcome:
        events = events or self.events

        def permit(tool: Tool, a: dict) -> bool:
            with self._permit_lock:  # one prompt at a time when instances run in parallel
                return events.permit(tool, a)

        events.on_tool_call(name, args)
        outcome = self.tools.execute(name, args, tool_ctx or self.tool_context(), permit,
                                     self.cfg["tools"]["max_result_chars"])
        events.on_tool_result(outcome)
        return outcome

    def agent(self, ctx: ContextManager | None = None, events: Events | None = None) -> "Agent":
        return Agent(self, ctx or self.ctx, events or self.events)

    def close(self) -> None:
        self.pool.close()
        self.project_rag.close()
        self.session_rag.close()


class Agent:
    """One user turn: generate, run tool calls, repeat until a plain answer."""

    def __init__(self, h: Harness, ctx: ContextManager, events: Events):
        self.h = h
        self.ctx = ctx
        self.events = events
        self.tool_ctx = h.tool_context(ctx)

    def build(self, query: str | None, extra_system: str = "", tools: bool = True) -> Built:
        built = self.ctx.build(query, extra_system, tools)
        if self.ctx.needs_refresh(built):
            moved = self.ctx.refresh(self.h.summarize)
            if moved:
                self.events.on_refresh(moved, built)
                built = self.ctx.build(query, extra_system, tools)
        return built

    def respond(self, user_msg: str, auto_tools: bool | None = None, extra_system: str = "",
                stream: bool = True) -> str:
        auto = self.h.auto_tools if auto_tools is None else auto_tools
        session = self.ctx.session
        session.add("user", user_msg)
        max_steps = self.h.cfg["tools"]["max_steps"]
        known = self.h.tools.param_orders()
        out, failures = "", 0
        for step in range(max_steps + 1):
            # Two unreadable calls in a row: stop offering tools for this turn rather
            # than spending every remaining step on the same mistake.
            tools_now = auto and step < max_steps and failures < MAX_PARSE_FAILURES
            extra = extra_system
            if auto and not tools_now:
                extra += ("\nYour tool calls could not be read, so tools are off. Answer now in plain text."
                          if failures >= MAX_PARSE_FAILURES else
                          "\nTool limit reached. Answer now with what you have; do not call tools.")
            built = self.build(user_msg, extra.strip(), tools=tools_now)
            out = self.h.generate(built.prompt, stream=stream, events=self.events)
            if not tools_now:
                break
            try:
                call = parse_tool_call(out, known)
            except ToolCallError as e:
                failures += 1
                self.events.on_notice(f"unreadable tool call: {e}")
                session.add("assistant", out.strip())
                session.add("tool", self._call_help(e))
                continue
            if call is None:
                break
            failures = 0
            session.add("assistant", call.raw, tool=call.name)
            outcome = self.h.run_tool(call.name, call.args, self.tool_ctx, self.events)
            session.add("tool", f"({call.name}): {outcome.text}", tool=call.name, ok=outcome.ok)
        out = out.strip()
        session.add("assistant", out)
        return out

    def _call_help(self, e: ToolCallError) -> str:
        """Feedback for an unreadable call: a correct example for the tool the model
        wanted. It avoids placeholder tags like <tool>…</tool>, which models echo back."""
        tool = self.h.tools.get(e.name) if e.name else None
        msg = (f"ERROR: your tool call could not be read ({e}). Write it as JSON inside tool tags, "
               f"exactly like this example but with your own values:\n{self.h.tools.example(tool)}")
        if tool:
            msg += f"\nUsage: {tool.usage()}"
        return msg
