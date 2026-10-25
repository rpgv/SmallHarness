"""LiteRT-LM backend (Google AI Edge, https://github.com/google-ai-edge/LiteRT-LM).

Modes ([litert].mode):
  python - the `litert-lm-api` package (`import litert_lm`): Engine -> create_conversation
           -> send_message / send_message_async. A fresh conversation is opened per call
           because the harness, not the runtime, owns the context window.
  server - `litert-lm serve`, an OpenAI-compatible server (see server.py): either one
           the harness launches ([litert.server].launch) or one at base_url.
  cli    - `litert-lm run MODEL --prompt ...` per call. Each call reloads the model and
           the prompt is a command-line argument (the only way `run` accepts it), so
           this is slow and visible in `ps`; it is used only when chosen explicitly.
  auto   - server if [litert.server].launch or base_url is set, else python if the
           package is installed. Never falls back to cli.

The Python API is young and its signatures have shifted between releases, so the
calls below are defensive. If your installed version differs, adjust `_open_engine`
and `_extract_text` only.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Iterator

from .base import Backend, apply_stop

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Longest prompt we pass as one argument: Linux caps a single argument at 128 KiB,
# Windows caps the whole command line at 32767 characters.
ARG_LIMIT = 30_000 if os.name == "nt" else 120_000

NO_RUNTIME = """no LiteRT-LM runtime configured. Choose one:
  server  set [litert.server].launch = true (or base_url for a running `litert-lm serve`)  (recommended)
  python  pip install litert-lm-api
  cli     set [litert].mode = "cli"   (slow: reloads the model per call; prompt visible in `ps`)"""


def resolve_mode(cfg: dict) -> str:
    mode = cfg.get("mode", "auto")
    if mode not in ("auto", "python", "server", "cli"):
        raise RuntimeError(f"unknown [litert].mode {mode!r}")
    if mode != "auto":
        return mode
    srv = cfg.get("server", {})
    if srv.get("launch") or srv.get("base_url"):
        return "server"
    if importlib.util.find_spec("litert_lm") is not None:
        return "python"
    raise RuntimeError(NO_RUNTIME)


def _extract_text(resp: Any) -> str:
    """send_message returns {"role": ..., "content": [{"type": "text", "text": ...}]}."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        content = resp.get("content", resp.get("text", ""))
        if isinstance(content, list):
            return "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        return str(content)
    for attr in ("text", "content"):
        if hasattr(resp, attr):
            return _extract_text(getattr(resp, attr))
    return str(resp)


def extract_output(stdout: str, pattern: str = "") -> str:
    """Strip terminal escapes; with `pattern`, keep only its first group (or whole match)."""
    text = _ANSI.sub("", stdout).replace("\r", "")
    if not pattern:
        return text.strip()
    m = re.search(pattern, text, re.S | re.M)
    if not m:
        raise RuntimeError("[litert.cli].output_regex did not match the command output; "
                           "run `smallharness check --probe` to see the raw output")
    return (m.group(1) if m.groups() else m.group(0)).strip()


class LiteRTBackend(Backend):
    name = "litert"

    def __init__(self, model_path: str, cfg: dict, mode: str, context_tokens: int = 4096):
        if mode not in ("python", "cli"):
            raise ValueError(mode)
        self.model_path = model_path
        self.context_tokens = context_tokens
        self.cfg = cfg
        self.mode = mode
        self.memory = "resident" if mode == "python" else "per_call"
        self._lm = None
        self._engine = None
        if mode == "cli":
            self._check_cli_config()

    # ---------------------------------------------------------------- loading
    def load(self) -> None:
        if self.mode == "cli":
            exe = self.cfg["cli"]["command"][0]
            if not shutil.which(exe):
                raise RuntimeError(f"`{exe}` is not on PATH ([litert.cli].command)")
            return
        if not self.model_path:
            raise RuntimeError("no model path: set [model].path or pass --model")
        try:
            import litert_lm  # type: ignore
        except ImportError:
            raise RuntimeError("litert_lm not installed: pip install litert-lm-api") from None
        self._lm = litert_lm
        self._engine = self._open_engine()

    def _open_engine(self):
        lm = self._lm
        accel = self.cfg.get("accelerator", "cpu").upper()
        backend_enum = getattr(getattr(lm, "Backend", None), accel, None)
        attempts = []
        # The window size kwarg is not in every version; older ones raise TypeError
        # and the next attempt runs without it (the engine then uses its default).
        if backend_enum is not None:
            attempts.append(lambda: lm.Engine(self.model_path, backend=backend_enum,
                                              max_num_tokens=self.context_tokens))
            attempts.append(lambda: lm.Engine(self.model_path, backend=backend_enum))
        attempts.append(lambda: lm.Engine(self.model_path, max_num_tokens=self.context_tokens))
        attempts.append(lambda: lm.Engine(self.model_path))
        last: Exception | None = None
        for make in attempts:
            try:
                engine = make()
                return engine.__enter__() if hasattr(engine, "__enter__") else engine
            except TypeError as e:
                last = e
        raise RuntimeError(f"could not open LiteRT-LM engine: {last}")

    def _conversation(self):
        conv = self._engine.create_conversation()
        return conv.__enter__() if hasattr(conv, "__enter__") else conv

    # ------------------------------------------------------------- generation
    def generate(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> str:
        if self.mode == "cli":
            proc = self.run_command(prompt, max_tokens)
            if proc.returncode != 0:
                raise RuntimeError(f"LiteRT-LM command failed ({proc.returncode}): {proc.stderr[-500:]}")
            return apply_stop(extract_output(proc.stdout, self.cfg["cli"].get("output_regex", "")), stop)
        conv = self._conversation()
        try:
            return apply_stop(_extract_text(conv.send_message(prompt)), stop)
        finally:
            if hasattr(conv, "__exit__"):
                conv.__exit__(None, None, None)

    def stream(self, prompt: str, max_tokens: int, stop: list[str] | None = None) -> Iterator[str]:
        if self.mode != "python":
            yield self.generate(prompt, max_tokens, stop)
            return
        conv = self._conversation()
        if not hasattr(conv, "send_message_async"):
            yield apply_stop(_extract_text(conv.send_message(prompt)), stop)
            return
        text, sent = "", 0
        try:
            for chunk in conv.send_message_async(prompt):
                text += _extract_text(chunk)
                cut = apply_stop(text, stop)
                if len(cut) > sent:
                    yield cut[sent:]
                    sent = len(cut)
                if len(cut) < len(text):  # hit a stop sequence
                    break
        finally:
            if hasattr(conv, "__exit__"):
                conv.__exit__(None, None, None)

    # -------------------------------------------------------------------- cli
    def _check_cli_config(self) -> None:
        c = self.cfg["cli"]
        cmd = c.get("command") or []
        if not cmd:
            raise RuntimeError("cli mode needs [litert.cli].command, e.g. "
                               '["litert-lm", "run", ...] (see `litert-lm run --help`)')
        joined = " ".join(cmd)
        via = c.get("prompt_via", "stdin")
        needs = {"arg": "{prompt}", "file": "{prompt_file}"}.get(via)
        if via not in ("arg", "file", "stdin"):
            raise RuntimeError(f"[litert.cli].prompt_via must be stdin, file or arg, not {via!r}")
        if needs and needs not in joined:
            raise RuntimeError(f"prompt_via = {via!r} but [litert.cli].command has no {needs}")
        if "{model}" in joined and not self.model_path:
            raise RuntimeError("[litert.cli].command uses {model} but [model].path is not set")

    def run_command(self, prompt: str, max_tokens: int) -> subprocess.CompletedProcess:
        """Run the configured command once. `max_tokens` is not passed: `litert-lm run`
        has no output-length flag (--max-num-tokens sizes the KV cache, i.e. the context).
        The prompt goes in as an argument, by stdin, or via a private temp file; only
        stdin and file keep it out of `ps`."""
        c = self.cfg["cli"]
        via = c.get("prompt_via", "stdin")
        subst = {"model": self.model_path, "accelerator": self.cfg.get("accelerator", "cpu"),
                 "context_tokens": str(self.context_tokens), "prompt": "", "prompt_file": ""}
        tmp = None
        try:
            if via == "arg":
                if len(prompt) > ARG_LIMIT:
                    raise RuntimeError(f"prompt is {len(prompt)} chars, too long for a command-line "
                                       "argument; set [litert.cli].prompt_via to stdin or file")
                subst["prompt"] = prompt
            elif via == "file":
                fd, tmp = tempfile.mkstemp(prefix="smallharness-", suffix=".txt")  # mode 0600
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(prompt)
                subst["prompt_file"] = tmp
            argv = [a.format(**subst) for a in c["command"]]
            return subprocess.run(argv, input=prompt if via == "stdin" else None,
                                  stdin=None if via == "stdin" else subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=c.get("timeout", 600))
        finally:
            if tmp:
                os.unlink(tmp)

    def close(self) -> None:
        if self._engine is not None and hasattr(self._engine, "__exit__"):
            self._engine.__exit__(None, None, None)
        self._engine = None
