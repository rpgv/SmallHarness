"""How the terminal shows the harness's events: streamed text, tool calls and results, and the permission question."""
from __future__ import annotations

import json
import sys

from .. import ui
from ..agent import Events
from ..tools import Tool, ToolOutcome


class TerminalEvents(Events):
    def __init__(self):
        self.spinner = ui.Spinner()
        self.streaming = False
        self.printed = False

    def on_text(self, chunk: str) -> None:
        if not self.streaming:
            self.spinner.stop()
            sys.stdout.write(ui.accent("◆ "))
            self.streaming = self.printed = True
        sys.stdout.write(chunk)
        sys.stdout.flush()

    def on_text_end(self) -> None:
        if self.streaming:
            sys.stdout.write("\n")
            self.streaming = False

    def on_tool_call(self, name: str, args: dict) -> None:
        self.spinner.stop()
        shown = ", ".join(f"{k}={json.dumps(v)[:60]}" for k, v in args.items())
        color = ui.risky if name.startswith("risky_") else ui.sky
        print(f"{color('▸')} {color(ui.bold(name))}({shown})")

    def on_tool_result(self, o: ToolOutcome) -> None:
        lines = o.text.splitlines() or [""]
        color = ui.pale if not o.ok else ui.dim
        preview = "\n    ".join(lines[:4])
        more = f"\n    {ui.dim(f'… +{len(lines) - 4} lines')}" if len(lines) > 4 else ""
        print(f"  {ui.steel('└')} {color(preview)}{more}")
        self.spinner.start("thinking")

    def on_refresh(self, moved: int, before) -> None:
        self.spinner.stop()
        print(ui.steel(f"↻ context at {before.usage:.0%}: moved {moved} turns to summary + session RAG"))
        self.spinner.start("thinking")

    def on_notice(self, msg: str) -> None:
        self.spinner.stop()
        print(ui.dim(f"· {msg}"))

    def permit(self, tool: Tool, args: dict) -> bool:
        self.spinner.stop()
        lines = [ui.risky(tool.name) + ui.dim("  (type 1: always asks)")]
        for k, v in args.items():
            val = v if isinstance(v, str) else json.dumps(v)
            val_lines = str(val).splitlines() or [""]
            lines.append(f"{ui.sky(k)}: {val_lines[0]}")
            lines += [f"  {ln}" for ln in val_lines[1:12]]
            if len(val_lines) > 12:
                lines.append(ui.dim(f"  … +{len(val_lines) - 12} lines"))
        print(ui.box(lines, "Permission required", ui.risky))
        ok = ui.confirm("Allow this call?", tty_only=False, color=ui.risky)
        if ok:
            self.spinner.start("running " + tool.name)
        return ok
