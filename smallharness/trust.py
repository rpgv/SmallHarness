"""Workspace trust.

A project can carry code that the harness would run: tool files in
`.smallharness/tools/` (imported at startup) and commands in its `smallharness.toml`
(the model server, the command-line runtime, OCR, extra tool folders). It can also
name servers that would receive the user's prompts, files or Hugging Face token.
Opening someone else's repository must not run its code or send data elsewhere, so
these are used only after the user trusts the workspace.

Trust is stored per workspace with a fingerprint of that code. Any change, such as
a new tool file or an edited command after a `git pull`, means asking again.
Settings from the user's own config (`~/.config/smallharness/config.toml`, or a file
passed with --config) are always trusted.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Settings that name programs to run or folders of code to import, where downloads
# (with the user's Hugging Face token) go, and where prompts and indexed files are sent.
CODE_KEYS = ("litert.server.command", "litert.cli.command", "rag.ocr.pdf_text",
             "rag.ocr.pdf_render", "rag.ocr.image", "tools.extra_dirs", "models.hf_endpoint",
             "litert.server.base_url", "ollama.base_url", "rag.embed.base_url")


def trust_file() -> Path:
    env = os.environ.get("SMALLHARNESS_TRUST_FILE")
    return Path(env).expanduser() if env else Path.home() / ".config" / "smallharness" / "trusted.json"


def tool_files(workspace: Path) -> list[Path]:
    """Every .py file in the workspace tools folder, including `_helpers.py` that tools may import."""
    d = workspace / ".smallharness" / "tools"
    return sorted(d.glob("*.py")) if d.is_dir() else []


@dataclass
class WorkspaceCode:
    """The code a workspace asks the harness to run."""
    workspace: Path
    tools: list[Path] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)   # CODE_KEYS set by the workspace

    def __bool__(self) -> bool:
        return bool(self.tools or self.settings)

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for f in self.tools:
            h.update(f.name.encode() + b"\0")
            try:
                h.update(f.read_bytes())
            except OSError:
                h.update(b"<unreadable>")
            h.update(b"\0")
        h.update(json.dumps(self.settings, sort_keys=True, default=str).encode())
        return h.hexdigest()

    def describe(self) -> list[str]:
        lines = []
        if self.tools:
            lines.append(f"tool files in .smallharness/tools/: {', '.join(f.name for f in self.tools)}")
        for k, v in self.settings.items():
            lines.append(f"smallharness.toml sets {k} = {json.dumps(v)}")
        return lines


def _load() -> dict[str, str]:
    try:
        data = json.loads(trust_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_trusted(code: WorkspaceCode) -> bool:
    """True if there is nothing to trust, or the user trusted exactly this code."""
    return not code or _load().get(str(code.workspace)) == code.fingerprint()


def grant(code: WorkspaceCode) -> None:
    """Trust this workspace's code as it is now. Written atomically, readable by the user only."""
    data = _load()
    data[str(code.workspace)] = code.fingerprint()
    path = trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".trusted-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
