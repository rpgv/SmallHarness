"""Recipe mode in the terminal: turn a recipe, section by section, into code or text."""
from __future__ import annotations

import os
import shlex
import subprocess

from .. import ui
from ..recipe import (
    RecipeRunner,
    find_recipe,
    list_recipes,
    load_recipe,
    recipes_dir,
    save_recipe,
)
from .shell import Shell

HELP = [
    ("/list", "saved recipes"),
    ("/new NAME [to=FILE] [text|code]", "write a recipe (sections separated by empty lines)"),
    ("/show NAME", "its sections and their state"),
    ("/edit NAME", "edit it in $EDITOR"),
    ("/write NAME|PATH [to=FILE] [text|code] [all] [only=N,M] [parallel]",
     "write the sections (asks once for risky_write_file); text = Markdown prose"),
    ("/library [LANGUAGE [WORDS]]", "the code library: languages, or one language's snippets (search with WORDS)"),
    ("/library show|remove LANGUAGE ID", "show a snippet's code, or take it out of the library"),
]


class RecipeShell(Shell):
    name = "recipe"
    about = "turn a recipe, section by section, into code or Markdown text; each section alone"
    hint = "/new NAME to write a recipe, /write NAME to turn it into code or text"
    help = HELP

    def banner(self) -> None:
        super().banner()
        names = list_recipes(self.h.cfg)
        if names:
            print(ui.dim(f"  recipes: {', '.join(names)}"))

    def cmd_list(self, _):
        names = list_recipes(self.h.cfg)
        if not names:
            print(ui.dim("  no saved recipes; /new NAME, or /write PATH for a recipe file"))
        for name in names:
            try:
                r = load_recipe(self.h.cfg, recipes_dir(self.h.cfg) / f"{name}.md")
            except ValueError as e:
                print(f"  {ui.sky(name)}  {ui.err(str(e))}")
                continue
            print(f"  {ui.sky(name)}  → {r.target} ({r.mode})  "
                  + ui.dim(f"{r.written()}/{len(r.sections)} section(s) written"))

    def cmd_new(self, arg):
        from ..notemode import write_text
        words = arg.split()
        if not words:
            print(ui.err("usage: /new NAME [to=FILE] [text|code]"))
            return
        name, opts, flags = words[0], _opts(words[1:]), set(words[1:])
        target, mode = opts.get("to", ""), _mode(flags)
        if (recipes_dir(self.h.cfg) / f"{name}.md").exists():
            print(ui.err(f"recipe {name} exists; /edit {name}"))
            return
        print(ui.dim("  one section per paragraph, separated by an empty line: what to write"
                     + ("" if target else "; start with `target: FILE` or pass to=FILE")))
        text = write_text(f"target: {target}\n\n" if target else "", "recipe")
        if not text:
            print(ui.dim("  cancelled"))
            return
        save_recipe(self.h.cfg, name, text, target, mode or "")
        self.cmd_show(name)

    def cmd_edit(self, arg):
        path = find_recipe(self.h.cfg, arg)
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            print(ui.dim(f"  no $EDITOR set: edit {path}"))
            return
        subprocess.run([*shlex.split(editor), str(path)], check=False)
        self.cmd_show(arg)

    def cmd_show(self, arg):
        words = arg.split()
        if not words:
            print(ui.err("usage: /show NAME"))
            return
        r = load_recipe(self.h.cfg, find_recipe(self.h.cfg, words[0]), _opts(words[1:]).get("to"))
        print(ui.accent(f"RECIPE {r.name}") + ui.dim(f"  → {r.target} · {r.mode}"))
        for s in r.sections:
            state = r.state(s)
            mark = {"done": ui.ok("✓"), "failed": ui.err("!"), "changed": ui.pale("~")}.get(state, " ")
            first, *rest = s.text.split("\n")
            print(f"  {mark} {s.num}. {first}" + ui.dim(f"  (+{len(rest)} line(s))" if rest else ""))
            if s.status == "failed" and s.note:
                print(ui.dim(f"       {s.note}"))

    def cmd_write(self, arg):
        words = arg.split()
        if not words:
            print(ui.err("usage: /write NAME|PATH [to=FILE] [text|code] [all] [only=N,M] [parallel]"))
            return
        opts, flags = _opts(words[1:]), set(words[1:])
        only = [int(n) for n in opts["only"].split(",")] if "only" in opts else None
        return self.write(words[0], opts.get("to"), _mode(flags), "all" in flags, only, "parallel" in flags)

    def write(self, name: str, target: str | None = None, mode: str | None = None, everything: bool = False,
              only: list[int] | None = None, parallel: bool = False, assume_yes: bool = False) -> int:
        recipe = load_recipe(self.h.cfg, find_recipe(self.h.cfg, name), target, mode)
        runner = RecipeRunner(self.h, recipe, self.ev, only=only, everything=everything)
        print(ui.dim(f"  recipe {recipe.name} → {recipe.target} ({recipe.mode}): {len(runner.todo)} of "
                     f"{len(recipe.sections)} section(s) to write, each by a model call that sees only its section"
                     + (" · in parallel" if parallel else "")))

        def start(s):
            print(ui.accent(f"▶ section {s.num}: ") + s.text.split("\n")[0][:100])
            if not parallel:
                self.ev.spinner.start("writing")

        def end(s):
            self.ev.spinner.stop()
            self.ev.on_text_end()
            if s.reused:
                print("  " + ui.ok(f"♻ section {s.num}: {s.note}"))
            elif s.status == "done":
                kept = f" · library {s.snippet}" if s.snippet else ""
                print("  " + ui.ok(f"✓ section {s.num}: {len(s.code.splitlines())} line(s)") + ui.dim(kept))
            else:
                print("  " + ui.err(f"section {s.num} FAILED: {s.note}"))

        try:
            res = runner.run(permit=(lambda: True) if assume_yes else None, on_start=start, on_end=end,
                             parallel=parallel)
        finally:
            self.ev.spinner.stop()
        if res.reason == "denied":
            print(ui.pale("  not allowed: nothing was written"))
            return 1
        if res.reason == "nothing to write":
            print(ui.dim("  every section is written already; add `all` or only=N to write again"))
            return 0
        lines = [f"written: {', '.join(map(str, res.written)) or 'none'}"]
        if res.failed:
            lines.append(f"failed:  {', '.join(map(str, res.failed))}  (kept in the file under a comment; "
                         f"/write {name} only=N to try again)")
        if res.reason == "interrupted":
            lines.append("interrupted")
        print(ui.box(lines, f"Recipe {recipe.name} → {recipe.target}", ui.deep))
        return 1 if res.failed or res.reason != "finished" else 0


    # ------------------------------------------------------------- library
    def cmd_library(self, arg):
        from ..snippets import libraries, open_library
        words = arg.split()
        if not words:
            langs = libraries(self.h.cfg)
            if not langs:
                print(ui.dim("  the code library is empty: recipes in code mode fill it"))
            for key in langs:
                lib = open_library(self.h.cfg, key)
                print(f"  {ui.sky(key)}  " + ui.dim(f"{len(lib.all())} snippet(s)"))
            print(ui.dim("  /library LANGUAGE [WORDS] to list or search one"))
            return
        if words[0] in ("show", "remove") and len(words) == 3:
            lib = open_library(self.h.cfg, words[1])
            if lib is None:
                print(ui.err(f"no code library for {words[1]!r}"))
                return
            if words[0] == "show":
                sn = lib.get(words[2])
                print(ui.accent(sn.id) + ui.dim(f"  {sn.description} · used by {', '.join(sn.recipes)}"))
                print(lib.code(sn.id).rstrip())
            else:
                lib.remove(words[2])
                print(ui.ok(f"  ✓ removed {words[2]}; recipes that used it write that section again"))
            return
        lib = open_library(self.h.cfg, words[0])
        if lib is None:
            print(ui.err(f"no code library for {words[0]!r}; /library lists them"))
            return
        found = lib.search(" ".join(words[1:]), 10) if len(words) > 1 else lib.all()
        if not found:
            print(ui.dim("  nothing matches" if len(words) > 1 else "  empty"))
        for sn in found:
            print(f"  {ui.sky(sn.id)}  {sn.description}" + ui.dim(f"  · {', '.join(sn.recipes)}"))


def _opts(words: list[str]) -> dict[str, str]:
    return dict(w.split("=", 1) for w in words if "=" in w)


def _mode(flags: set[str]) -> str | None:
    """`text` or `code` among the words, else None (the recipe's own, or by its target)."""
    return "text" if "text" in flags else "code" if "code" in flags else None
