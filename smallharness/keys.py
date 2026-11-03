"""API keys for outside services (Gemini, NCBI for PubMed).

A key comes from its environment variable if that is set, else from the keys file:
`~/.config/smallharness/keys.toml` (or $SMALLHARNESS_KEYS_FILE), which lives outside
every project, so it is never committed, and is readable by the user only.

    gemini = "AIza…"
    ncbi = "…"

Keys are never part of the settings, never shown in full, and never read from a
workspace: a repository cannot plant a key that would receive your searches."""
from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path

from .errors import Invalid

# name -> (environment variables, what it is for)
KEYS: dict[str, tuple[tuple[str, ...], str]] = {
    "gemini": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "Gemini, a larger model to ask (/gemini)"),
    "ncbi": (("NCBI_API_KEY",), "PubMed: optional, raises NCBI's limit from 3 to 10 requests a second"),
}


def keys_file() -> Path:
    env = os.environ.get("SMALLHARNESS_KEYS_FILE")
    return Path(env).expanduser() if env else Path.home() / ".config" / "smallharness" / "keys.toml"


def _read() -> dict[str, str]:
    try:
        data = tomllib.loads(keys_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def _known(name: str) -> str:
    if name not in KEYS:
        raise Invalid(f"no key named {name!r}; keys: {', '.join(KEYS)}")
    return name


def get(name: str) -> str:
    """The key, or "" if there is none."""
    for var in KEYS[_known(name)][0]:
        if os.environ.get(var):
            return os.environ[var].strip()
    return _read().get(name, "").strip()


def where(name: str) -> str:
    """Where the key comes from: an environment variable, the keys file, or ""."""
    for var in KEYS[_known(name)][0]:
        if os.environ.get(var):
            return f"${var}"
    return str(keys_file()) if _read().get(name) else ""


def masked(name: str) -> str:
    key = get(name)
    return f"…{key[-4:]}" if len(key) > 8 else ("set" if key else "")


def put(name: str, value: str) -> None:
    """Save a key in the keys file (an empty value removes it). Written atomically,
    readable by the user only."""
    _known(name)
    value = value.strip()
    if any(c in value for c in "\n\r\0"):
        raise Invalid("a key is one line")
    data = _read()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    path = keys_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keys-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# SmallHarness API keys. Keep this file private.\n")
            for k, v in sorted(data.items()):
                f.write(f"{k} = {json.dumps(v)}\n")       # a JSON string is a valid TOML basic string
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def status() -> list[dict]:
    return [{"name": n, "about": about, "set": bool(get(n)), "masked": masked(n), "from": where(n)}
            for n, (_, about) in KEYS.items()]
