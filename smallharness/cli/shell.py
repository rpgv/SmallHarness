"""What every terminal mode shares: the input loop, help, switching modes, status, and
the model and settings commands. Each mode (research, planner, recipe) adds its own
commands and decides what plain text means."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from .. import __version__, ui
from ..agent import Harness
from ..config import DEFAULTS, changed_keys, flatten, is_secret, parse_value
from ..engines import engine
from ..errors import HarnessError
from .events import TerminalEvents
from .model_cmds import delete_model, print_models, pull_model

SWITCH = "switch"      # what a command returns to go back to the mode menu

COMMON_HELP = [
    ("/help", "show this help"),
    ("/mode", "go back to the mode menu (research, planner, recipe)"),
    ("/status", "model, instances, search"),
    ("/model [NAME|PATH|ID] [ctx=N|auto …]", "show the model, or switch to another one"),
    ("/models, /pull NAME, /delete NAME", "list, download or delete models"),
    ("/config [show|set K=V|reload]", "show settings, change them for this run, or re-read smallharness.toml"),
    ("/resources", "RAM and how many model instances fit"),
    ("/exit", "quit"),
]


class Shell:
    name = ""                       # the mode, as the banner shows it
    about = ""                      # one line: what this mode is for
    hint = ""                       # what to type
    help: list[tuple[str, str]] = []

    def __init__(self, h: Harness, events: TerminalEvents, no_color: bool = False):
        self.h = h
        self.ev = events
        self.no_color = no_color

    # ------------------------------------------------------------ display
    def banner(self) -> None:
        cfg = self.h.cfg
        ws = str(cfg.workspace).replace(str(Path.home()), "~")
        if len(ws) > 60:
            ws = "…" + ws[-59:]
        print(ui.box([
            ui.accent(ui.bold(f"SmallHarness · {self.name}")) + ui.dim(f"  v{__version__}"),
            ui.dim(f"  {self.about}"),
            "",
            f"  {ui.steel('model  ')}  {self.model_line()}",
            f"  {ui.steel('workdir')}  {ws}",
            "",
            ui.dim(f"  {self.hint} · /help for commands · /mode to switch"),
        ], color=ui.deep))
        note = cfg.context.note
        if note and not note.startswith("auto:"):
            print(ui.warn(note))
        be = self.h.pool.instances[0] if self.h.pool.instances else None
        if be is not None and be.memory == "per_call":
            print(ui.warn("command-line mode: the model is reloaded on every call (slow, no streaming).\n"
                          "  Prefer `litert-lm serve` with [litert.server].base_url."))

    def model_line(self) -> str:
        cfg = self.h.cfg
        be = self.h.pool.instances[0] if self.h.pool.instances else None
        mode = f"{be.name}({getattr(be, 'mode', '')})".replace("()", "") if be else "not loaded"
        return f"{cfg.model_name} · {mode} · {cfg.context_tokens} ctx"

    def status_line(self) -> str:
        pool = self.h.pool
        return ui.dim(f"  {self.h.cfg.model_name} · instances {len(pool.instances)}/{pool.capacity} · "
                      f"search {self.h.search_mode()}")

    # --------------------------------------------------------------- loop
    def run(self) -> str | None:
        """Until /exit (returns None) or /mode (returns SWITCH)."""
        self.banner()
        while True:
            try:
                line = self._read()
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            if not line.strip():
                continue
            try:
                out = self.command(line) if line.startswith("/") else self.text(line.strip())
                if out is False:          # /exit (not 0, which a command may return as its result)
                    return None
                if out == SWITCH:
                    return SWITCH
            except KeyboardInterrupt:
                self.ev.spinner.stop()
                self.ev.on_text_end()
                print(ui.pale("\n└ interrupted"))
            except HarnessError as e:      # expected: say what is wrong, no traceback name
                self.ev.spinner.stop()
                print(ui.err(str(e)))
            except Exception as e:
                self.ev.spinner.stop()
                print(ui.err(f"{type(e).__name__}: {e}"))

    def _read(self) -> str:
        print()
        lines = [input(ui.accent(ui.bold(f"{self.name} › ")))]
        while lines[-1].endswith("\\"):
            lines[-1] = lines[-1][:-1]
            lines.append(input(ui.dim(". ")))
        return "\n".join(lines)

    def text(self, line: str):
        """What plain text (not a /command) does in this mode."""
        print(ui.dim(f"  {self.hint}; /help for commands"))

    def command(self, line: str):
        name, _, arg = line[1:].partition(" ")
        fn = getattr(self, f"cmd_{name.replace('-', '_')}", None)
        if fn is None:
            print(ui.err(f"unknown command /{name}; try /help"))
            return None
        return fn(arg.strip())

    # ----------------------------------------------------------- commands
    def cmd_help(self, _):
        rows = self.help + COMMON_HELP
        w = max(len(c) for c, _ in rows)
        for c, d in rows:
            print(f"  {ui.sky(c.ljust(w))}  {ui.dim(d)}")

    def cmd_exit(self, _):
        return False

    cmd_quit = cmd_exit

    def cmd_mode(self, _):
        return SWITCH

    def cmd_status(self, _):
        print(self.status_line())
        print(ui.dim(f"  project index {len(self.h.project_rag)} chunks · pool {self.h.pool.status()} · "
                     f"tokens {self.h.counter.describe()}"))

    def cmd_resources(self, _):
        print_context(self.h)
        print(self.h.pool.report().describe())
        print(ui.dim(f"pool: {self.h.pool.status()}"))

    def cmd_models(self, _):
        print_models(self.h.cfg)

    def cmd_pull(self, arg):
        words = arg.split()
        spec = next((w for w in words if w != "force"), "")
        if not spec:
            print(ui.err("usage: /pull NAME [force]   (see /models for names)"))
            return
        name = pull_model(self.h.cfg, spec, force="force" in words)
        if name:
            print(ui.dim(f"  load it with /model {name}"))

    def cmd_delete(self, arg):
        if not arg:
            print(ui.err("usage: /delete NAME   (see /models for names)"))
            return
        delete_model(self.h.cfg, arg, in_use=True)

    def cmd_model(self, arg):
        cfg = self.h.cfg
        if not arg:
            m, srv = cfg["model"], cfg["litert"]["server"]
            print(f"  {ui.steel('model')}     {self.model_line()}")
            print(f"  {ui.steel('path')}      {m['path'] or '-'}")
            eng = engine(cfg)
            if eng.kind == "litert" and eng.server_mode():
                print(f"  {ui.steel('server id')} {srv['model'] or '(first listed by the server)'}")
            if eng.kind == "ollama":
                print(f"  {ui.steel('ollama')}    {eng.model() or '(first listed)'} at {eng.server_url()}")
            print(f"  {ui.steel('template')}  {m['template']} · output {m['max_output_tokens']} tokens")
            print_context(self.h, indent="  ")
            print(ui.dim("  switch: /model PATH|ID [ctx=N|auto] [out=N] [template=plain|gemma] [name=X] [path=P] [mb=N]"))
            return
        values, warnings = engine(cfg).switch_values(shlex.split(arg))
        for w in warnings:
            print(ui.warn(w))
        self._apply(cfg.with_values(values), "loading model")

    def cmd_config(self, arg):
        cfg = self.h.cfg
        sub, _, rest = arg.partition(" ")
        rest = rest.strip()
        if sub in ("", "show"):
            files = ", ".join(str(f).replace(str(Path.home()), "~") for f in cfg.files) or "none (defaults)"
            print(f"  {ui.steel('files')}  {files}")
            flat, defaults = flatten(cfg.data), flatten(DEFAULTS)
            keys = [k for k in flat if k.startswith(rest + ".")] if rest else \
                [k for k in flat if flat[k] != defaults.get(k)]
            if not rest:
                print(ui.dim("  settings that differ from the defaults (/config show SECTION for all):"))
            for k in keys:
                value = '"…hidden…"' if is_secret(k) and flat[k] else json.dumps(flat[k])
                print(f"  {ui.sky(k)} = {value}")
            print(ui.dim("  /config set KEY=VALUE … (this run only) · /config reload (re-read the files)"))
        elif sub == "set":
            values = {}
            for w in shlex.split(rest):
                key, eq, val = w.partition("=")
                if not eq:
                    print(ui.err("usage: /config set section.key=value [section.key=value …]"))
                    return
                values[key.strip()] = parse_value(val.strip())
            try:
                data = cfg.with_values(values)
            except KeyError as e:
                print(ui.err(f"{e.args[0]}; see /config show SECTION"))
                return
            self._apply(data, "applying settings")
        elif sub == "reload":
            data, files = cfg.read_files()
            kept = sorted(flatten(cfg.overrides))
            if kept:
                print(ui.dim(f"  kept from command-line flags: {', '.join(kept)}"))
            if self._apply(data, "applying settings"):
                cfg.files = files
        else:
            print(ui.err("usage: /config [show [SECTION] | set KEY=VALUE … | reload]"))

    def _apply(self, data: dict, label: str) -> bool:
        cfg = self.h.cfg
        changed = sorted(changed_keys(cfg.data, data))
        if not changed:
            print(ui.dim("no changes"))
            return True
        self.ev.spinner.start(label)
        try:
            done = self.h.reconfigure(data)
        except RuntimeError as e:
            self.ev.spinner.stop()
            print(ui.err(str(e)))
            return False
        finally:
            self.ev.spinner.stop()
        ui.set_color(cfg["ui"]["color"] and not self.no_color)
        for k in changed:
            print(f"  {ui.sky(k)} = {json.dumps(flatten(cfg.data).get(k))}")
        for line in done:
            print(ui.ok(f"  ✓ {line}"))
        print(self.status_line())
        return True

    # ------------------------------------------------------------ helpers
    def busy(self, label: str):
        """`with self.busy("label"):` shows the spinner while the block runs."""
        shell = self

        class _Busy:
            def __enter__(self):
                shell.ev.spinner.start(label)

            def __exit__(self, *exc):
                shell.ev.spinner.stop()
        return _Busy()


def print_context(h: Harness, indent: str = "") -> None:
    """The context window in use, how it was chosen, and what the model file says."""
    from ..modelinfo import read_model_info
    cfg = h.cfg
    choice = cfg.context
    print(f"{indent}context  {choice.tokens} tokens" + (f" ({choice.note})" if choice.note else ""))
    info = read_model_info(cfg["model"]["path"]) if cfg["model"]["path"] else None
    if info:
        print(f"{indent}file     {info.describe()}")
        if info.kv_layers:
            print(f"{indent}         KV cache at {choice.tokens} tokens: {info.kv_mb(choice.tokens)} MB")
    note = engine(cfg).context_note(h.backend_mode())
    if note:
        print(ui.dim(f"{indent}         {note}"))


def print_index_result(res, total_chunks: int) -> None:
    print(ui.dim(f"{res.describe()}; the knowledge base has {total_chunks} chunks"))
    for f, why in res.skipped[:15]:
        print("  " + ui.warn(f"{f}: {why}"))
    if len(res.skipped) > 15:
        print(ui.dim(f"  … +{len(res.skipped) - 15} more skipped"))


def setup_readline(history: Path) -> None:
    try:
        import readline
    except ImportError:
        return
    try:
        readline.read_history_file(history)
    except OSError:
        pass
    readline.set_history_length(1000)
    import atexit
    atexit.register(lambda: _save_history(readline, history))


def _save_history(readline, history: Path) -> None:
    try:
        readline.write_history_file(history)
    except OSError:
        pass
