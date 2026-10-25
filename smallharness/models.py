"""Model store: a folder of model files, downloads from Hugging Face, and short names.

Folder ([models].dir): set it to put models anywhere. Unset, it is `models/` in the
SmallHarness checkout (git-ignored), or, for an installed package with no checkout,
the user data folder (~/.local/share/smallharness/models).

Short names: wherever a model path is taken (-m, /model, [model].path), a name that
is not a file is looked up in the folder: a catalog name (`gemma-4-E2B-it`), a file
name, or a file name without its suffix.

Downloads (`smallharness pull`) come from Hugging Face over HTTPS, stdlib only. The
file is written to `<name>.part`, resumed from there if interrupted, checked against
the size and SHA-256 that Hugging Face publishes, and only then renamed into place.
A token (HF_TOKEN, or [models].hf_token) is needed for gated models; it is sent to
the Hugging Face endpoint only, never to the CDN the download is redirected to.

Server mode: `litert-lm serve` serves models from its own store, not from files.
With [models].auto_import, a model file from here is imported into that store
(`litert-lm import FILE ID`, a hard link, so no extra disk space) under the file's
name without suffix, and that ID is what the server is asked for.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SUFFIXES = (".litertlm", ".task")
MANIFEST = "models.json"


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    repo: str
    file: str
    size_gb: float
    note: str = ""
    gated: bool = False


# Checked against Hugging Face (litert-community), CPU builds of each model.
CATALOG: dict[str, CatalogEntry] = {e.name.lower(): e for e in [
    CatalogEntry("gemma-4-E2B-it", "litert-community/gemma-4-E2B-it-litert-lm",
                 "gemma-4-E2B-it.litertlm", 2.59, "Gemma 4 E2B, instruction-tuned; 32k context"),
    CatalogEntry("gemma-4-E4B-it", "litert-community/gemma-4-E4B-it-litert-lm",
                 "gemma-4-E4B-it.litertlm", 3.66, "Gemma 4 E4B, instruction-tuned"),
    CatalogEntry("gemma3-1b-it", "litert-community/Gemma3-1B-IT",
                 "gemma3-1b-it-int4.litertlm", 0.58, "Gemma 3 1B, int4", gated=True),
    CatalogEntry("qwen3-0.6b", "litert-community/Qwen3-0.6B", "Qwen3-0.6B.litertlm", 0.61, "Qwen3 0.6B"),
    CatalogEntry("qwen2.5-1.5b-instruct", "litert-community/Qwen2.5-1.5B-Instruct",
                 "Qwen2.5-1.5B-Instruct_multi-prefill-seq_q8_ekv4096.litertlm", 1.6,
                 "Qwen2.5 1.5B Instruct, int8, 4k context"),
    CatalogEntry("granite-4.0-350m", "litert-community/granite-4.0-350m-litert-lm",
                 "granite-4.0-350m_q8_ekv1280.litertlm", 0.47, "IBM Granite 4.0 350M, int8; smallest"),
]}


class PullError(RuntimeError):
    pass


# ------------------------------------------------------------------ folder
def repo_checkout() -> Path | None:
    """The SmallHarness source checkout this code runs from, if it is one."""
    root = Path(__file__).resolve().parent.parent
    try:
        if 'name = "smallharness"' in (root / "pyproject.toml").read_text():
            return root
    except OSError:
        pass
    return None


def _user_data_dir() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "smallharness" / "models"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "smallharness" / "models"


def models_dir(mcfg: dict) -> tuple[Path, str]:
    """The model folder and where that choice came from."""
    if mcfg.get("dir"):
        return Path(mcfg["dir"]).expanduser().resolve(), "[models].dir"
    root = repo_checkout()
    if root and os.access(root, os.W_OK):
        return root / "models", "SmallHarness checkout"
    return _user_data_dir(), "user data folder"


def model_files(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("*") if p.suffix in SUFFIXES and p.is_file()) if d.is_dir() else []


def catalog_entry(name: str) -> CatalogEntry | None:
    return CATALOG.get(name.lower())


def resolve(name: str, d: Path) -> Path | None:
    """A short name -> its model file in the folder, or None. Paths are not names."""
    if not name or "/" in name or "\\" in name:
        return None
    candidates = []
    entry = catalog_entry(name)
    if entry:
        candidates.append(d / entry.file)
    candidates += [d / name] + [d / (name + s) for s in SUFFIXES]
    for c in candidates:
        if c.suffix in SUFFIXES and c.is_file():
            return c
    low = name.lower()
    return next((p for p in model_files(d) if low in (p.name.lower(), p.stem.lower())), None)


def resolve_model_path(value: str, mcfg: dict) -> str:
    """[model].path as given: a file path, a short name for a file in the folder, or
    (left as is) a server model ID."""
    if not value:
        return value
    p = Path(value).expanduser()
    if p.is_file():
        return str(p)
    found = resolve(value, models_dir(mcfg)[0])
    return str(found) if found else str(p)


# --------------------------------------------------------------- manifest
def _manifest(d: Path) -> dict:
    try:
        data = json.loads((d / MANIFEST).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_manifest(d: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".models-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, d / MANIFEST)


def _record(d: Path, filename: str, entry: dict) -> None:
    data = _manifest(d)
    data[filename] = entry
    _write_manifest(d, data)


@dataclass
class LocalModel:
    path: Path
    size: int
    name: str                      # what to pass to -m / /model
    source: str = ""               # Hugging Face repo it came from, if pulled here
    max_context: int | None = None


def list_local(d: Path) -> list[LocalModel]:
    from .modelinfo import read_model_info
    by_file = {e.file: e for e in CATALOG.values()}
    manifest = _manifest(d)
    out = []
    for p in model_files(d):
        entry = by_file.get(p.name)
        info = read_model_info(str(p))
        out.append(LocalModel(p, p.stat().st_size, entry.name if entry else p.stem,
                              manifest.get(p.name, {}).get("repo") or (entry.repo if entry else ""),
                              info.max_context if info else None))
    return out


@dataclass
class Deleted:
    files: list[Path]              # what was removed: the model and/or its unfinished .part
    freed: int                     # bytes, 0 if the file is still linked from the LiteRT-LM store
    store_id: str = ""             # ID in the LiteRT-LM store that still holds the same file


def find_deletable(name: str, d: Path) -> list[Path]:
    """The files `delete` would remove for a name: the model in the folder and any
    unfinished download of it. Only files in the model folder, never other paths."""
    found = resolve(name, d)
    files = [found] if found else []
    if not found and name and "/" not in name and "\\" not in name:
        entry = catalog_entry(name)
        stems = {name.lower()} | ({entry.file.lower()} if entry else set())
        files += [p for p in d.glob("*.part") if p.is_file()
                  and (p.stem.lower() in stems or Path(p.stem).stem.lower() in stems)]
    if found:
        part = found.with_name(found.name + ".part")
        if part.is_file():
            files.append(part)
    return files


def delete(name: str, d: Path) -> Deleted:
    """Remove a model (and an unfinished download of it) from the folder and the manifest."""
    files = find_deletable(name, d)
    if not files:
        raise FileNotFoundError(f"no model {name!r} in {d}")
    store_id, freed = "", 0
    for p in files:
        size = p.stat().st_size
        if p.suffix != ".part":
            entry = litert_store() / server_id(str(p)) / "model.litertlm"
            try:
                if entry.is_file() and os.path.samefile(entry, p):
                    store_id, size = server_id(str(p)), 0
            except OSError:
                pass
        p.unlink()
        freed += size
    manifest = _manifest(d)
    if any(p.name in manifest for p in files):
        for p in files:
            manifest.pop(p.name, None)
        _write_manifest(d, manifest)
    return Deleted(files, freed, store_id)


# ---------------------------------------------------------------- download
@dataclass(frozen=True)
class Source:
    repo: str
    file: str
    revision: str = "main"

    @property
    def page(self) -> str:
        return f"https://huggingface.co/{self.repo}"


def parse_spec(spec: str) -> tuple[Source, CatalogEntry | None]:
    """A catalog name, `hf:OWNER/REPO/FILE`, `OWNER/REPO/FILE`, or a huggingface.co file URL."""
    spec = spec.strip()
    entry = catalog_entry(spec)
    if entry:
        return Source(entry.repo, entry.file), entry
    revision = "main"
    if spec.startswith(("http://", "https://")):
        parts = urllib.parse.urlparse(spec).path.strip("/").split("/")
        if len(parts) < 5 or parts[2] not in ("resolve", "blob"):
            raise PullError(f"not a Hugging Face file URL: {spec}")
        repo, revision, file = "/".join(parts[:2]), parts[3], "/".join(parts[4:])
    else:
        parts = spec.removeprefix("hf:").strip("/").split("/")
        if len(parts) < 3:
            names = ", ".join(e.name for e in CATALOG.values())
            raise PullError(f"unknown model {spec!r}: use a name ({names}) or hf:OWNER/REPO/FILE")
        repo, file = "/".join(parts[:2]), "/".join(parts[2:])
    if not file.endswith(SUFFIXES):
        raise PullError(f"{file} is not a model file ({' or '.join(SUFFIXES)})")
    return Source(repo, file, revision), None


def hf_token(mcfg: dict) -> str:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or mcfg.get("hf_token", "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # the 3xx itself is the answer: it carries the file's size and hash


class _DropAuthOffHost(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but do not send the token to another host (the CDN)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


@dataclass
class Remote:
    url: str
    size: int | None
    sha256: str | None
    commit: str = ""


def _headers(token: str) -> dict:
    h = {"User-Agent": "smallharness"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _http_error(e: urllib.error.HTTPError, src: Source, token: str) -> PullError:
    if e.code in (401, 403):
        how = ("your token has no access: accept the model's license on its page while signed in"
               if token else "it is gated: accept its license on its page, then set HF_TOKEN to a token "
                             "from https://huggingface.co/settings/tokens")
        return PullError(f"{src.repo} needs permission ({e.code}); {how}\n  {src.page}")
    if e.code == 404:
        return PullError(f"no file {src.file!r} in {src.repo} (revision {src.revision}); "
                         f"see {src.page}/tree/{src.revision}")
    return PullError(f"Hugging Face returned {e.code} for {src.repo}/{src.file}")


def inspect(src: Source, mcfg: dict) -> Remote:
    """Size and SHA-256 of a file on Hugging Face, without downloading it."""
    endpoint = (mcfg.get("hf_endpoint") or "https://huggingface.co").rstrip("/")
    url = f"{endpoint}/{src.repo}/resolve/{urllib.parse.quote(src.revision)}/{urllib.parse.quote(src.file)}"
    token = hf_token(mcfg)
    req = urllib.request.Request(url, headers=_headers(token), method="HEAD")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=mcfg.get("timeout", 60)) as r:
            headers = r.headers
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise _http_error(e, src, token) from None
        headers = e.headers
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise PullError(f"cannot reach {endpoint}: {getattr(e, 'reason', e)}") from None
    size = headers.get("X-Linked-Size") or headers.get("Content-Length")
    etag = (headers.get("X-Linked-Etag") or headers.get("ETag") or "").removeprefix("W/").strip('"')
    return Remote(url, int(size) if size and size.isdigit() else None,
                  etag.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", etag) else None,
                  headers.get("X-Repo-Commit", ""))


@dataclass
class Pulled:
    path: Path
    size: int
    already: bool = False          # was already in the folder
    resumed_from: int = 0          # bytes kept from an interrupted download
    verified: bool = False         # SHA-256 checked


Progress = Callable[[int, int | None], None]


def pull(spec: str, mcfg: dict, progress: Progress | None = None, force: bool = False,
         remote: Remote | None = None) -> Pulled:
    """Download a model into the folder. Resumes a `.part` file left by an interrupted run."""
    src, _ = parse_spec(spec)
    d = models_dir(mcfg)[0]
    d.mkdir(parents=True, exist_ok=True)
    dest = d / Path(src.file).name
    remote = remote or inspect(src, mcfg)
    if dest.exists() and not force and (remote.size is None or dest.stat().st_size == remote.size):
        return Pulled(dest, dest.stat().st_size, already=True)

    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if remote.size is not None and have > remote.size:
        part.unlink()
        have = 0
    need = (remote.size or 0) - have
    free = shutil.disk_usage(d).free
    if need > free:
        raise PullError(f"not enough disk space in {d}: need {need / 1e9:.2f} GB, {free / 1e9:.2f} GB free")

    digest = hashlib.sha256()
    if have:
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
    token = hf_token(mcfg)
    headers = _headers(token)
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(remote.url, headers=headers)
    opener = urllib.request.build_opener(_DropAuthOffHost)
    resumed_from = 0
    try:
        with opener.open(req, timeout=mcfg.get("timeout", 60)) as r:
            if have and r.status == 206:
                resumed_from, mode = have, "ab"
            else:  # no range support: start over
                digest, have, mode = hashlib.sha256(), 0, "wb"
            done = have
            with open(part, mode) as f:
                while block := r.read(1 << 20):
                    f.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress:
                        progress(done, remote.size)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:  # the range is past the end: the .part is stale
            part.unlink()
            return pull(spec, mcfg, progress, force, remote)
        raise _http_error(e, src, token) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise PullError(f"download interrupted ({getattr(e, 'reason', e)}); "
                        f"run the same pull again to resume") from None

    size = part.stat().st_size
    if remote.size is not None and size != remote.size:
        raise PullError(f"download incomplete: {size} of {remote.size} bytes; run it again to resume")
    if remote.sha256 and digest.hexdigest() != remote.sha256:
        part.unlink()
        raise PullError("the download does not match the SHA-256 Hugging Face publishes; it was "
                        "deleted, run the pull again")
    os.replace(part, dest)
    _record(d, dest.name, {"repo": src.repo, "file": src.file, "revision": src.revision,
                           "commit": remote.commit, "sha256": remote.sha256 or digest.hexdigest(),
                           "size": size, "pulled": time.strftime("%Y-%m-%d %H:%M:%S")})
    return Pulled(dest, size, resumed_from=resumed_from, verified=bool(remote.sha256))


# ------------------------------------------------------------ server mode
def litert_store() -> Path:
    """Where `litert-lm import` keeps models (LITERT_LM_DIR, default ~/.litert-lm)."""
    base = os.environ.get("LITERT_LM_DIR")
    return (Path(base).resolve() if base else Path.home() / ".litert-lm") / "models"


def server_id(path: str) -> str:
    """The ID a model file is imported under: its file name without the suffix."""
    return Path(path).stem


def ensure_imported(path: str, model_id: str, exe: str = "litert-lm", timeout: int = 600) -> bool:
    """Import a model file into the LiteRT-LM store unless it is already there.
    Returns True if it was imported now."""
    dest = litert_store() / model_id.replace("/", "--") / "model.litertlm"
    if dest.is_file():
        try:
            if os.path.samefile(dest, path) or dest.stat().st_size == os.path.getsize(path):
                return False
        except OSError:
            pass
        raise RuntimeError(f"the LiteRT-LM store already has a different model with ID {model_id!r}. "
                           f"Remove it (`litert-lm delete {model_id}`) or serve this file under "
                           "another ID: /model NAME id=OTHER")
    if not shutil.which(exe):
        raise RuntimeError(f"`{exe}` is not on PATH, so {Path(path).name} cannot be imported for the "
                           "server; install the LiteRT-LM CLI (pip install litert-lm)")
    try:
        proc = subprocess.run([exe, "import", str(path), model_id], capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"`{exe} import` took longer than {timeout}s") from None
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise RuntimeError(f"`{exe} import {Path(path).name} {model_id}` failed: {' '.join(tail)}")
    return True
