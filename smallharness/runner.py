"""Running relics from the web interface: a suggested command for a file (from its
shebang line or its type), and runs whose output streams back to the page.

A run is the user's own command, typed or accepted on the page, so it does not ask
again; it runs in the workspace, in its own process group so Stop ends everything it
started, and is stopped after [relics].run_timeout seconds."""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .errors import Invalid, NotFound

# suffix -> command, with {file} (the quoted path) and {stem} (a scratch output path)
BY_SUFFIX = {
    ".py": "{python} {file}", ".sh": "bash {file}", ".bash": "bash {file}", ".zsh": "zsh {file}",
    ".js": "node {file}", ".mjs": "node {file}", ".cjs": "node {file}", ".ts": "npx --yes tsx {file}",
    ".rb": "ruby {file}", ".pl": "perl {file}", ".php": "php {file}", ".lua": "lua {file}", ".r": "Rscript {file}",
    ".go": "go run {file}", ".java": "java {file}", ".swift": "swift {file}", ".kts": "kotlinc -script {file}",
    ".c": "cc {file} -o {stem} && {stem}", ".cpp": "c++ {file} -o {stem} && {stem}", ".cc": "c++ {file} -o {stem} && {stem}",
    ".rs": "rustc {file} -o {stem} && {stem}", ".sql": "sqlite3 :memory: < {file}",
    ".html": "{python} -m webbrowser -t {url}", ".htm": "{python} -m webbrowser -t {url}",
}
MAX_OUTPUT = 200_000          # characters kept of a run's output


def _python(workspace: Path) -> str:
    for venv in (".venv", "venv"):
        p = workspace / venv / "bin" / "python"
        if p.is_file():
            return shlex.quote(f"{venv}/bin/python")
    return "python3"


def suggest(workspace: Path, rel: str) -> str:
    """The command that would run this file, or "" if there is no obvious one."""
    p = workspace / rel
    quoted = shlex.quote(rel)
    try:
        with p.open("rb") as f:
            first = f.readline(200).decode("utf-8", "replace").strip()
    except OSError:
        first = ""
    if first.startswith("#!"):
        if os.access(p, os.X_OK):
            return f"./{quoted}"
        parts = first[2:].split()
        if parts:
            prog = parts[1:] if Path(parts[0]).name == "env" else parts
            if prog:
                return " ".join([*prog, quoted])
    template = BY_SUFFIX.get(p.suffix.lower())
    if not template:
        return ""
    stem = shlex.quote(f"/tmp/smallharness-{p.stem}")
    return template.format(python=_python(workspace), file=quoted, stem=stem, url=shlex.quote(p.resolve().as_uri()))


class Run:
    def __init__(self, command: str, cwd: Path, timeout: float):
        self.id = uuid.uuid4().hex[:12]
        self.command, self.started = command, time.time()
        self.output: list[str] = []
        self.code: int | None = None
        self.stopped = False
        self._cond = threading.Condition()
        self.proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1,
                                     start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        threading.Thread(target=self._read, daemon=True).start()
        self._timer = threading.Timer(timeout, self.stop, kwargs={"why": f"stopped after {timeout:.0f} s"})
        self._timer.daemon = True
        self._timer.start()

    def _read(self) -> None:
        size = 0
        for line in self.proc.stdout:
            with self._cond:
                if size < MAX_OUTPUT:
                    self.output.append(line[: MAX_OUTPUT - size])
                    size += len(line)
                self._cond.notify_all()
        code = self.proc.wait()
        self._timer.cancel()
        with self._cond:
            self.code = code
            self._cond.notify_all()

    def stop(self, why: str = "stopped") -> None:
        if self.code is None:
            self.stopped = why
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def follow(self):
        """The output as it comes, then the end: ("out", text) … ("end", code)."""
        sent = 0
        while True:
            with self._cond:
                while sent == len(self.output) and self.code is None:
                    self._cond.wait(0.5)
                chunk, sent = "".join(self.output[sent:]), len(self.output)
                code = self.code
            if chunk:
                yield "out", chunk
            if code is not None and sent == len(self.output):
                yield "end", code
                return


class Runner:
    """The runs started from the page; finished ones are dropped after a while."""

    def __init__(self, workspace: Path, cfg: dict):
        self.workspace, self.cfg = workspace, cfg
        self.runs: dict[str, Run] = {}

    def start(self, command: str) -> Run:
        command = command.strip()
        if not command:
            raise Invalid("which command?")
        now = time.time()
        for rid, r in list(self.runs.items()):
            if r.code is not None and now - r.started > 3600:
                del self.runs[rid]
        run = Run(command, self.workspace, float(self.cfg.get("run_timeout", 300)))
        self.runs[run.id] = run
        return run

    def get(self, run_id: str) -> Run:
        if run_id not in self.runs:
            raise NotFound(f"no run {run_id}")
        return self.runs[run_id]

    def stop_all(self) -> None:
        for r in self.runs.values():
            r.stop()
