"""Research mode in the terminal: ask questions of your files and notes."""
from __future__ import annotations

import shlex

from .. import ui
from ..research import Research
from .shell import Shell, print_index_result

HELP = [
    ("QUESTION", "ask: answered from the knowledge base, citing excerpts as [n]"),
    ("/add PATH", "add a file or folder to the knowledge base (text, PDF, images)"),
    ("/files", "the files and notes in the knowledge base (◇ marks the scope)"),
    ("/remove SOURCE", "take a file or note out of the knowledge base"),
    ("/note TEXT", "save a note into the knowledge base (#tags optional)"),
    ("/notes", "note mode: list, search, edit and tag notes (/exit comes back)"),
    ("/scope [SOURCE …|all]", "limit questions to some files, folders or notes"),
    ("/find WORDS", "the excerpts a question would get, without asking the model"),
    ("/bibliography [N]", "the references [n] so far, or reference N in full"),
    ("/attach N, /detach N", "keep answer N in the context of later questions (CAG), or not"),
    ("/answers", "this session's answers; ◆ marks the attached ones"),
]


class ResearchShell(Shell):
    name = "research"
    about = "ask questions of your files and notes; answers cite the excerpts they rest on"
    hint = "type a question"
    help = HELP

    def __init__(self, h, events, no_color: bool = False):
        super().__init__(h, events, no_color)
        self.r = Research(h)

    def banner(self) -> None:
        super().banner()
        n = len(self.h.project_rag.sources)
        print(ui.dim(f"  knowledge base: {n} source(s), {len(self.h.project_rag)} chunks · search {self.h.search_mode()}"))
        if not self.h.embedder:
            print(ui.warn("keyword search only: `pip install fastembed`, or set [rag.embed], to search by meaning"))

    # -------------------------------------------------------------- asking
    def text(self, line: str):
        self.ask(line)

    def ask(self, question: str) -> None:
        self.ev.printed = False
        with self.busy("searching"):
            answer = self.r.ask(question, self.ev)
        self.ev.on_text_end()
        if not self.ev.printed:
            print(ui.accent("◆ ") + answer.text)
        refs = " ".join(f"[{n}]" for n in answer.refs) or "none"
        scope = f" · scope {', '.join(answer.scope)}" if answer.scope else ""
        print(ui.dim(f"  A{answer.num} · refs {refs}{scope} · /attach {answer.num} keeps it in mind · /bibliography"))

    def cmd_find(self, arg):
        if not arg:
            print(ui.err("usage: /find WORDS"))
            return
        refs = self.r.find(arg)
        if not refs:
            print(ui.dim("  nothing matches"))
        for r in refs:
            first = " ".join(r.text.split())[:110]
            print(f"  {ui.sky(f'[{r.num}]')} {ui.dim(r.cite())}\n      {first}")

    # ------------------------------------------------------------- library
    def cmd_add(self, arg):
        if not arg:
            print(ui.err("usage: /add PATH"))
            return
        for path in shlex.split(arg):
            with self.busy("indexing"):
                res = self.r.add(path, progress=lambda label: setattr(self.ev.spinner, "label", label))
            print_index_result(res, len(self.h.project_rag))

    def cmd_files(self, _):
        items = self.r.library()
        if not items:
            print(ui.dim("  the knowledge base is empty: /add PATH, or /note TEXT"))
        for x in items:
            mark = ui.accent("◇") if self.r.scope and x["in_scope"] else " "
            kind = ui.dim("note") if x["kind"] == "note" else ui.dim("file")
            label = x["title"] if x["kind"] == "note" else x["source"]
            print(f"  {mark} {kind} {ui.sky(x['source']) if x['kind'] == 'note' else ''} {label}"
                  + ui.dim(f"  {x['chunks']} chunk(s)"))
        print(ui.dim(f"  scope: {', '.join(self.r.scope) if self.r.scope else 'everything'}"))

    def cmd_remove(self, arg):
        if not arg:
            print(ui.err("usage: /remove SOURCE   (see /files)"))
            return
        self.r.remove(arg)
        print(ui.ok(f"  ✓ removed {arg}"))

    def cmd_note(self, arg):
        if not arg:
            print(ui.err("usage: /note TEXT"))
            return
        from ..notemode import NoteMode
        NoteMode(self.h.notes).save(arg)

    def cmd_notes(self, arg):
        from ..notemode import NoteMode
        mode = NoteMode(self.h.notes, inside_chat=True)
        if arg:
            mode.command("/" + arg)
            return
        mode.run()
        print(ui.dim("  back in research"))

    def cmd_scope(self, arg):
        if arg:
            self.r.set_scope([] if arg == "all" else shlex.split(arg))
        print(ui.dim(f"  questions search: {', '.join(self.r.scope) if self.r.scope else 'everything'}"))

    # -------------------------------------------------- references and CAG
    def cmd_bibliography(self, arg):
        if arg.isdigit():
            n = int(arg)
            if not 1 <= n <= len(self.r.refs):
                print(ui.err(f"no reference {n}"))
                return
            r = self.r.refs[n - 1]
            print(f"  {ui.sky(f'[{r.num}]')} {r.cite()}\n")
            print(r.text.strip())
            return
        if not self.r.refs:
            print(ui.dim("  no references yet: ask a question"))
            return
        for r in self.r.refs:
            print(f"  {ui.sky(f'[{r.num}]')} {r.cite()}  " + ui.dim(" ".join(r.text.split())[:80]))
        print(ui.dim(f"  in full: /bibliography N, or {self.r.bibliography_path}"))

    def cmd_attach(self, arg):
        for n in _numbers(arg):
            a = self.r.attach(n)
            print(ui.ok(f"  ✓ A{a.num} attached: later questions keep it in mind"))

    def cmd_detach(self, arg):
        for n in _numbers(arg):
            a = self.r.detach(n)
            print(ui.ok(f"  ✓ A{a.num} detached"))

    def cmd_answers(self, _):
        if not self.r.answers:
            print(ui.dim("  no answers yet"))
        for a in self.r.answers:
            mark = ui.accent("◆") if a.attached else " "
            print(f"  {mark} A{a.num} {a.question}\n      " + ui.dim(" ".join(a.text.split())[:110]))


def _numbers(arg: str) -> list[int]:
    """`2`, `A2`, `2,3` -> answer numbers."""
    nums = []
    for word in arg.replace(",", " ").split():
        word = word.upper().lstrip("A")
        if word.isdigit():
            nums.append(int(word))
    if not nums:
        raise ValueError("give an answer number, e.g. /attach 2")
    return nums
