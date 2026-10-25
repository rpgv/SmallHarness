"""Terminal rendering: colors, boxes, spinner, title. Stdlib only.

The palette is shades of blue. Red is reserved for risky (type 1) tools, so it
always means "this can change something".
"""
from __future__ import annotations

import itertools
import os
import shutil
import sys
import threading
import time
from pathlib import Path

_ON = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def set_color(enabled: bool) -> None:
    global _ON
    _ON = enabled and sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str):
    return lambda s: f"\033[{code}m{s}\033[0m" if _ON else str(s)


dim, bold = _c("2"), _c("1")
accent = _c("38;5;33")        # primary blue: prompt, answers, frames
sky = _c("38;5;117")          # light blue: tool names, keys, success
pale = _c("38;5;153")         # pale blue: warnings, notices that need a look
deep = _c("38;5;25")          # dark blue: plans, secondary frames
steel = _c("38;5;67")         # grey-blue: refreshes, metadata
risky = _c("1;31")            # red: risky tools only

ok = sky
warn = lambda s: pale(f"! {s}")
err = lambda s: bold(pale(f"✗ {s}"))

TITLE = [
    r" ____                  _ _ _   _",
    r"/ ___| _ __ ___   __ _| | | | | | __ _ _ __ _ __   ___  ___ ___",
    r"\___ \| '_ ` _ \ / _` | | | |_| |/ _` | '__| '_ \ / _ \/ __/ __|",
    r" ___) | | | | | | (_| | | |  _  | (_| | |  | | | |  __/\__ \__ \ ",
    r"|____/|_| |_| |_|\__,_|_|_|_| |_|\__,_|_|  |_| |_|\___||___/___/",
]
_TITLE_SHADES = ("38;5;117", "38;5;75", "38;5;33", "38;5;27", "38;5;25")  # light to dark


def title() -> str:
    """The ASCII title, shaded from light to dark blue."""
    if width() < max(len(r) for r in TITLE) + 2:
        return accent(bold("SmallHarness"))
    return "\n".join(_c(code)(row.rstrip()) for code, row in zip(_TITLE_SHADES, TITLE))


def show_title(mode: str, marker: Path) -> bool:
    """mode: always | never | first (only until `marker` exists; creates it)."""
    if mode == "always":
        return True
    if mode != "first":
        return False
    if marker.exists():
        return False
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass
    return True


def width() -> int:
    return min(shutil.get_terminal_size((90, 24)).columns, 100)


def _visible(s: str) -> int:
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def box(lines: list[str], title: str = "", color=dim) -> str:
    w = width() - 2
    top = "┌─" + (f" {title} " if title else "")
    top += "─" * max(0, w - _visible(top)) + "┐"
    out = [color(top)]
    for line in lines:
        for chunk in _wrap(line, w - 3):
            out.append(color("│") + " " + chunk + " " * max(0, w - 2 - _visible(chunk)) + color("│"))
    out.append(color("└" + "─" * (w - 1) + "┘"))
    return "\n".join(out)


def _wrap(line: str, w: int) -> list[str]:
    if _visible(line) <= w or "\033" in line:
        return [line]
    return [line[i:i + w] for i in range(0, len(line), w)] or [""]


def confirm(question: str, assume_yes: bool = False, refused: str = "", tty_only: bool = True,
            color=None) -> bool:
    """Ask a yes/no question; no answer is no. With `tty_only`, also no when input is not a
    terminal (a pipe cannot agree to a download by accident). `refused` is printed on no."""
    if assume_yes:
        return True
    try:
        ok = (not tty_only or sys.stdin.isatty()) and \
            input((color or pale)(f"{question} [y/N] ")).strip().lower() in ("y", "yes")
    except EOFError:
        ok = False
    if not ok and refused:
        print(dim(refused))
    return ok


def meter(frac: float, cells: int = 10) -> str:
    n = max(0, min(cells, round(frac * cells)))
    color = sky if frac < 0.6 else accent if frac < 0.85 else (lambda s: bold(pale(s)))
    return color("■" * n) + dim("·" * (cells - n))


class Spinner:
    FRAMES = "◐◓◑◒"

    def __init__(self, label: str = "thinking"):
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = 0.0

    def start(self, label: str | None = None) -> None:
        if label:
            self.label = label
        if not sys.stdout.isatty() or self._thread:
            return
        self._stop.clear()
        self.started = time.time()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.wait(0.12):
                break
            secs = int(time.time() - self.started)
            sys.stdout.write(f"\r\033[K{accent(frame)} {steel(self.label)} {dim(f'{secs}s · ctrl-c stops')}")
            sys.stdout.flush()

    def stop(self) -> None:
        if self._thread:
            self._stop.set()
            self._thread.join()
            self._thread = None
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
