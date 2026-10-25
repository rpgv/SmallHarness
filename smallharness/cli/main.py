"""The command line: arguments, subcommands, the workspace trust question, and the entry point."""
from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from .. import __version__, ui
from ..agent import Events, Harness
from ..config import Config
from ..engines import engine
from ..errors import HarnessError
from .events import TerminalEvents
from .model_cmds import delete_model, print_models, pull_model
from .shell import SWITCH, print_context, print_index_result, setup_readline

PROBE_PROMPT = "Reply with exactly one word: pong"


def check(h: Harness, probe: bool) -> int:
    """RAM report, and with probe=True one timed test call showing what the harness receives."""
    import time
    pool = h.pool
    be = pool._peek()
    print(f"backend  {be.name} ({be.mode or '-'}), RAM counted as {be.memory}")
    print_context(h)
    print(pool.report().describe())
    print(f"search   {h.search_mode()}")
    if not probe:
        return 0
    pool.ensure_one()
    if h.embedder and not probe_embedder(h):
        return 1
    be = pool.instances[0]
    t0 = time.time()
    if be.memory == "per_call":
        from ..backends.litert import _ANSI, extract_output
        proc = be.run_command(PROBE_PROMPT, 16)
        secs = time.time() - t0
        argv = [a if PROBE_PROMPT not in a else "<prompt>" for a in proc.args]
        print(f"command  {' '.join(argv)}\nexit     {proc.returncode} after {secs:.1f}s")
        plain = lambda t: _ANSI.sub("", t).replace("\r", "").splitlines()
        print(ui.box(plain(proc.stdout)[-40:] or ["(empty)"], "raw stdout, colors removed (last 40 lines)"))
        if proc.stderr.strip():
            print(ui.box(plain(proc.stderr)[-15:], "stderr (last 15 lines)"))
        try:
            print(f"answer   {extract_output(proc.stdout, be.cfg['cli'].get('output_regex', ''))!r}")
        except RuntimeError as e:
            print(ui.err(str(e)))
            return 1
        print(ui.dim("If the answer has log lines around it, set [litert.cli].output_regex to capture "
                     "only the reply.\nEvery call pays this load time again; server mode avoids it."))
        return 0 if proc.returncode == 0 else 1
    answer = be.generate(PROBE_PROMPT, 16)
    print(f"answer   {answer!r} after {time.time() - t0:.1f}s")
    return 0


def probe_embedder(h: Harness) -> bool:
    import time

    from ..context.embed import EmbedError, similarities
    t0 = time.time()
    try:
        vecs = h.embedder.embed_documents(["The cat sat on the mat.", "Quarterly revenue grew by 8%."])
        q = h.embedder.embed_query("where did the cat sit?")
    except EmbedError as e:
        print(ui.err(f"embeddings: {e}"))
        return False
    near, far = similarities(q, vecs)
    print(f"embed    {h.embedder.id}: {len(q)} dimensions, {time.time() - t0:.1f}s for 3 texts; "
          f"similarity related {near:.2f} vs unrelated {far:.2f}")
    if near <= far:
        print(ui.warn("the related sentence did not score higher; this model may not suit search"))
    elif far >= h.embedder.min_similarity:
        print(ui.dim(f"  unrelated text scores {far:.2f}; consider [rag.embed].min_similarity "
                     f"≈ {(near + far) / 2:.2f} for this model"))
    return True


# ------------------------------------------------------------------- entry
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="smallharness",
        description="Small local models (LiteRT-LM, or Ollama), used through three modes: research, "
                    "planner and recipe. Without a command, choose a mode.")
    p.add_argument("-m", "--model", help="a model file, a name from `smallharness models`, or (ollama) a model name")
    p.add_argument("-b", "--backend", choices=["litert", "ollama", "mock"])
    p.add_argument("-w", "--workspace", default=".", help="project directory (default: current)")
    p.add_argument("-c", "--config", help="config TOML (default: ./smallharness.toml)")
    p.add_argument("--ctx", type=int, help="model context window in tokens")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--trust", action="store_true",
                   help="trust this workspace's tools and commands for this run, without asking")
    p.add_argument("--version", action="version", version=f"smallharness {__version__}")
    sub = p.add_subparsers(dest="cmd")
    rs = sub.add_parser("research", help="ask questions of your files and notes (with a question: answer and exit)")
    rs.add_argument("question", nargs="*")
    ask = sub.add_parser("ask", help="one research question: answer from the knowledge base and exit")
    ask.add_argument("question", nargs="+")
    ix = sub.add_parser("index", help="add files or folders to the knowledge base")
    ix.add_argument("paths", nargs="+")
    pl = sub.add_parser("plan", help="the planner (with a goal: plan it, and optionally expand and run it)")
    pl.add_argument("goal", nargs="*")
    pl.add_argument("--expand", type=int, default=0, metavar="N", help="expand every step N passes")
    pl.add_argument("--run", choices=["fresh", "continuous"])
    pl.add_argument("--parallel", action="store_true", help="expand steps and run independent tasks in parallel")
    rc = sub.add_parser("recipe", help="recipe mode (with a name: write it and exit)")
    rc.add_argument("recipe", nargs="?", help="a saved recipe's name, or a recipe file in the workspace")
    rc.add_argument("--to", help="the file to write (default: the recipe's `target:` line)")
    kind = rc.add_mutually_exclusive_group()
    kind.add_argument("--text", action="store_true", help="write Markdown prose (default for .md targets)")
    kind.add_argument("--code", action="store_true", help="write code")
    rc.add_argument("--all", action="store_true", help="write every section again, not only new or changed ones")
    rc.add_argument("--only", help="write only these sections, e.g. 2,3")
    rc.add_argument("--parallel", action="store_true", help="write sections in parallel, as RAM allows")
    rc.add_argument("-y", "--yes", action="store_true", help="allow risky_write_file without asking")
    wb = sub.add_parser("web", help="open the visual interface in a browser (local web server)")
    wb.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this machine only)")
    wb.add_argument("--port", type=int, default=8765, help="port (0: any free port)")
    wb.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    wb.add_argument("--load", action="store_true",
                    help="load the model before the page opens, instead of on first use")
    nt = sub.add_parser("notes", help="note mode: take notes without loading a model")
    nt.add_argument("text", nargs="*", help="save this as a note and exit, instead of opening note mode")
    ck = sub.add_parser("check", help="report RAM and how many model instances fit")
    ck.add_argument("--probe", action="store_true", help="also send one test prompt and show the raw result")
    sub.add_parser("models", help="list downloaded models, and ones available to pull (with -b ollama: Ollama's)")
    pu = sub.add_parser("pull", help="download a model into the model folder (with -b ollama: into Ollama)")
    pu.add_argument("name", help="a name from `smallharness models`, hf:OWNER/REPO/FILE, or a Hugging Face file URL")
    pu.add_argument("--force", action="store_true", help="download again even if the file is there")
    pu.add_argument("-y", "--yes", action="store_true", help="download even if it will not fit in RAM")
    de = sub.add_parser("delete", help="delete a downloaded model from the model folder (with -b ollama: from Ollama)")
    de.add_argument("name", help="a name from `smallharness models`")
    de.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    return p


MODES = {"research": ("ask questions of your files and notes, with references", "ResearchShell"),
         "planner": ("plan code changes, expand the steps, run them", "PlannerShell"),
         "recipe": ("turn a section-by-section recipe into code or text", "RecipeShell")}


def choose_mode() -> str | None:
    """The mode menu: a number or a name; nothing (or end of input) quits."""
    print(ui.box([ui.accent(ui.bold("SmallHarness")) + ui.dim(f"  v{__version__}"), ""]
                 + [f"  {ui.sky(str(i))}  {ui.bold(name.ljust(9))} {ui.dim(about)}"
                    for i, (name, (about, _)) in enumerate(MODES.items(), 1)], color=ui.deep))
    names = list(MODES)
    while True:
        try:
            ans = input(ui.accent(ui.bold("mode › "))).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not ans or ans in ("q", "quit", "exit"):
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(names):
            return names[int(ans) - 1]
        match = [n for n in names if n.startswith(ans)]
        if len(match) == 1:
            return match[0]
        print(ui.dim(f"  1-{len(names)}, or a name: {', '.join(names)}"))


def run_modes(h: Harness, events, first: str | None, no_color: bool) -> int:
    """The chosen mode, then the menu again after /mode, until /exit."""
    from . import planner_mode, recipe_mode, research_mode
    classes = {"research": research_mode.ResearchShell, "planner": planner_mode.PlannerShell,
               "recipe": recipe_mode.RecipeShell}
    setup_readline(h.cfg.state.history)
    mode = first or choose_mode()
    while mode:
        if classes[mode](h, events, no_color).run() != SWITCH:
            break
        mode = choose_mode()
    return 0


def serve_web(h: Harness, host: str, port: int, open_browser: bool = True, load: bool = False) -> int:
    """The visual interface. The model loads on first use, so the knowledge base and
    notes work right away, or with `load` before the link is shown."""
    from ..web.server import ApiError, make_server
    try:
        server, url = make_server(h, host, port)
    except OSError as e:
        print(ui.err(f"cannot listen on {host}:{port} ({e}); try --port 0"))
        return 1
    if load:
        app = server.RequestHandlerClass.app
        print(ui.dim(f"  loading {h.cfg.model_name}…"))
        try:
            app.ensure_loaded()
            print(ui.dim(f"  loaded in {app.load_s:.1f} s"))
        except ApiError as e:
            print(ui.warn(f"{e}\n  the page still opens; the model loads when first needed"))
    if host not in ("127.0.0.1", "localhost"):
        print(ui.warn(f"listening on {host}: anyone who can reach this machine on the network can use "
                      "this page and its model, with no key; use it only on a network you trust"))
    print(ui.box([ui.accent(ui.bold("SmallHarness web")) + ui.dim(f"  v{__version__}"), "",
                  f"  {ui.steel('open')}  {url}", "",
                  ui.dim("  Ctrl-C stops the server")], color=ui.deep))
    if open_browser:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print(ui.dim("\n  stopped"))
    finally:
        server.server_close()
    return 0


def run_notes(cfg: Config, text: str = "") -> int:
    """`smallharness notes`: note mode without a Harness, so no model is loaded."""
    from ..notemode import NoteMode
    from ..notes import open_notes
    mode = NoteMode(open_notes(cfg))
    if text:
        try:
            mode.save(text)
        except ValueError as e:
            print(ui.err(str(e)))
            return 1
        return 0
    mode.run()
    return 0


def confirm_trust(cfg: Config, interactive: bool) -> None:
    """Ask before using code the workspace brings (tool files, commands in its
    smallharness.toml). Without an answer it stays withheld for this run."""
    code = cfg.untrusted
    if not code:
        return
    lines = [ui.risky("This workspace asks SmallHarness to run its own code or send data to servers it names:")]
    lines += [f"  {ln}" for ln in code.describe()]
    lines.append(ui.dim("Only trust projects you know. Trust is asked again if this code changes."))
    print(ui.box(lines, "Untrusted workspace", ui.risky))
    if interactive and ui.confirm(f"Trust {cfg.workspace} and run this code?", tty_only=False, color=ui.risky):
        cfg.grant_trust()
        print(ui.dim("  └ trusted"))
    else:
        print(ui.warn("Not trusted: workspace tools are not loaded and these settings are ignored "
                      "(use --trust, or answer y in an interactive session)."))


def _exit_on_signal(signum, _frame) -> None:
    """SIGTERM/SIGHUP end the run like Ctrl-D does, so cleanup runs and model
    servers the harness started are stopped."""
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict = {"model": {}, "ui": {}}
    if args.model:
        overrides["model"]["path"] = args.model
    if args.backend:
        overrides["model"]["backend"] = args.backend
    if args.ctx:
        overrides["model"]["context_tokens"] = args.ctx
    cfg = Config.load(Path(args.workspace), Path(args.config) if args.config else None, overrides,
                      trust_run=args.trust)
    ui.set_color(cfg["ui"]["color"] and not args.no_color)
    if args.cmd == "notes":
        return run_notes(cfg, " ".join(args.text))
    if args.cmd == "models":
        print_models(cfg)
        return 0
    if args.cmd == "pull":
        name = pull_model(cfg, args.name, force=args.force, assume_yes=args.yes)
        if name:
            flag = "-b ollama " if engine(cfg).kind == "ollama" else ""
            print(ui.dim(f"run it with: smallharness {flag}-m {name}"))
        return 0 if name else 1
    if args.cmd == "delete":
        return 0 if delete_model(cfg, args.name, assume_yes=args.yes) else 1
    interactive = args.cmd in (None, "research", "plan", "web", "recipe")
    confirm_trust(cfg, interactive and sys.stdin.isatty())
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, _exit_on_signal)

    events = Events() if args.cmd == "web" else TerminalEvents()
    h = Harness(cfg, events)
    try:
        return _run(h, cfg, args, events)
    except HarnessError as e:
        print(ui.err(str(e)))
        return 1
    finally:
        h.close()


def _run(h: Harness, cfg: Config, args, events: Events) -> int:
    if args.cmd == "web":
        return serve_web(h, args.host, args.port, not args.no_browser, args.load)
    if args.cmd == "check":
        try:
            return check(h, args.probe)
        except RuntimeError as e:
            print(ui.err(str(e)))
            return 1
    if args.cmd == "index":
        from ..research import Research
        r = Research(h)
        for p in args.paths:
            print(f"{p}: ", end="")
            print_index_result(r.add(p), len(h.project_rag))
        return 0

    try:
        report = h.pool.report()
        if not report.fits:
            print(ui.err(report.describe()))
            return 1
        events_spinner = getattr(events, "spinner", None)
        if events_spinner:
            events_spinner.start("loading model")
        try:
            h.start()
        finally:
            if events_spinner:
                events_spinner.stop()
    except RuntimeError as e:
        print(ui.err(f"cannot start model: {e}"))
        print(ui.dim("try the mock backend first: smallharness --backend mock"))
        return 1
    if args.cmd in ("ask", "research") and args.question:
        from .research_mode import ResearchShell
        shell = ResearchShell(h, events, args.no_color)
        shell.ask(" ".join(args.question))
        print(ui.dim(f"  references: {shell.r.bibliography_path}"))
        return 0
    if args.cmd == "plan" and args.goal:
        from .planner_mode import PlannerShell
        shell = PlannerShell(h, events, args.no_color)
        shell.text(" ".join(args.goal))
        if args.expand:
            shell.cmd_expand(f"times={args.expand}")
        if args.run:
            shell.cmd_run(args.run + (" parallel" if args.parallel else ""))
        return 0
    if args.cmd == "recipe" and args.recipe:
        from .recipe_mode import RecipeShell
        only = [int(n) for n in args.only.split(",")] if args.only else None
        mode = "text" if args.text else "code" if args.code else None
        return RecipeShell(h, events, args.no_color).write(args.recipe, args.to, mode, args.all, only,
                                                           args.parallel, args.yes)
    first = {"research": "research", "plan": "planner", "recipe": "recipe"}.get(args.cmd)
    return run_modes(h, events, first, args.no_color)
