"""Models from the terminal: list, download (with a progress line) and delete, on top of engines.py."""
from __future__ import annotations

import sys
import time

from .. import ui
from ..config import Config


def print_models(cfg: Config) -> None:
    from ..engines import engine
    eng = engine(cfg)
    lst = eng.listing()
    print(ui.dim(f"models in {lst.where}"))
    if lst.error:
        print(ui.err(lst.error))
        return
    if lst.models:
        w = max(len(m.name) for m in lst.models)
        for m in lst.models:
            mark = ui.accent("●") if m.active else " "
            ctx = f"{m.max_context} ctx" if m.max_context else "ctx ?"
            print(f" {mark} {ui.sky(m.name.ljust(w))}  {m.size / 1e9:5.2f} GB  {ctx:>10}  {ui.dim(m.about)}")
    else:
        print("   (none yet)")
    if lst.available:
        print(ui.dim("available to pull (smallharness pull NAME, or /pull NAME):"))
        w = max(len(a.name) for a in lst.available)
        for a in lst.available:
            gated = ui.pale("  gated: needs HF_TOKEN") if a.gated else ""
            print(f"   {a.name.ljust(w)}  {a.size_gb:5.2f} GB  {ui.dim(a.note)}{gated}")
    if eng.kind == "ollama":
        print(ui.dim("pull another with `smallharness -b ollama pull NAME` (names: ollama.com/library)"))
    print(ui.dim("load one with -m NAME or /model NAME"))


def delete_model(cfg: Config, name: str, assume_yes: bool = False, in_use: bool = False) -> bool:
    """Delete a model after asking. `in_use`: the model in cfg is loaded right now (the
    REPL), so it is not deleted. Returns True if it was deleted."""
    from ..engines import InUse, ModelError, engine
    eng = engine(cfg)
    try:
        d = eng.plan_delete(name, in_use)
    except InUse as e:
        print(ui.err(f"{e} (/model NAME)"))
        return False
    except ModelError as e:
        print(ui.err(f"{e} (see `smallharness models`)"))
        return False
    if d.active:
        print(ui.warn(f"{d.items[0]} is the model in your config"))
    size = f" ({d.size / 1e9:.2f} GB)" if d.size else ""
    print(f"delete {', '.join(d.items)}{size} from {d.where}")
    if not ui.confirm("Delete?", assume_yes, "not deleted (use --yes to skip the question)"):
        return False
    try:
        got = eng.delete(d)
    except ModelError as e:
        print(ui.err(str(e)))
        return False
    print(ui.ok("deleted" + (f", {got.freed / 1e9:.2f} GB freed" if got.freed else "")))
    if got.note:
        print(ui.dim(f"  {got.note}"))
    return True


def pull_model(cfg: Config, spec: str, force: bool = False, assume_yes: bool = False) -> str | None:
    """Download a model, after a RAM check where the size is known in advance. Returns
    the name to load it by."""
    from ..engines import ModelError, engine
    eng = engine(cfg)
    try:
        plan = eng.plan_pull(spec)
    except ModelError as e:
        print(ui.err(str(e)))
        return None
    print(plan.what + (f" · {plan.size / 1e9:.2f} GB" if plan.size else ""))
    if plan.warning:
        print(ui.warn(plan.warning))
    if plan.wont_run and not ui.confirm("Download anyway?", assume_yes, "not downloaded (use --yes to download anyway)"):
        return None
    bar = _Progress()
    try:
        got = eng.pull(plan, bar, force=force)
    except ModelError as e:
        bar.end()
        print(ui.err(str(e)))
        return None
    except KeyboardInterrupt:
        bar.end()
        print(ui.pale("interrupted; run the same pull again to resume"))
        return None
    bar.end()
    print(ui.dim(got.detail) if got.already else ui.ok(f"✓ {got.detail}"))
    if got.warning:
        print(ui.warn(got.warning))
    return got.name


class _Progress:
    """One updating line on a terminal; nothing when output is piped. A new total
    (Ollama reports each layer separately) starts a new line."""

    def __init__(self):
        self.last, self.tty = 0.0, sys.stdout.isatty()
        self.start, self.total = None, None

    def __call__(self, done: int, total: int | None) -> None:
        now = time.time()
        if self.start is not None and total != self.total:
            self.end()
            self.start = None
        if self.start is None:
            self.start, self.total = (done, now), total
        if not self.tty or (now - self.last < 0.2 and done != total):
            return
        self.last = now
        rate = (done - self.start[0]) / max(now - self.start[1], 1e-6)
        pct = f"{done / total * 100:5.1f}%  " if total else ""
        tot = f" / {total / 1e9:.2f}" if total else ""
        eta = f"  {int((total - done) / rate) // 60}m{int((total - done) / rate) % 60:02d}s left" \
            if total and rate > 0 and done < total else ""
        sys.stdout.write(f"\r  {pct}{done / 1e9:.2f}{tot} GB  {rate / 1e6:6.1f} MB/s{eta}   ")
        sys.stdout.flush()

    def end(self) -> None:
        if self.tty and self.start is not None:
            sys.stdout.write("\n")
