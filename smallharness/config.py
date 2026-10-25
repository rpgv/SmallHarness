"""Configuration: defaults deep-merged with `smallharness.toml` from the workspace."""
from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import trust

DEFAULTS: dict[str, Any] = {
    "model": {
        "backend": "litert",          # litert | ollama | mock
        "path": "",                   # path to a .litertlm / .task model file (ollama: a model name)
        "name": "",                   # display name (defaults to file stem)
        "context_tokens": "auto",     # tokens, or "auto": read the maximum from the model file
        "auto_context_max": 8192,     # cap for "auto": long prompts are slow to process on CPU
        "max_output_tokens": 768,     # reserved for generation
        "template": "plain",          # plain (chat-API runtimes) | gemma (raw text)
    },
    "litert": {
        "mode": "auto",               # auto (server, else python) | server | python | cli
        "accelerator": "cpu",         # cpu | gpu (python mode)
        "server": {                   # OpenAI-compatible server: `litert-lm serve`
            # launch = true: the harness starts one server per model instance on
            # 127.0.0.1 and stops it on exit. launch = false: connect to base_url.
            "launch": False,
            "command": ["litert-lm", "serve", "--host", "127.0.0.1", "--port", "{port}"],
            "port": 9379,             # first port tried; later instances get free ports
            "startup_timeout": 120,
            "base_url": "",           # e.g. http://127.0.0.1:9379 (/v1 is added if no path)
            "model": "",              # imported model ID (`litert-lm list`); empty = first listed
            "api_key": "",
            "max_concurrent": 1,      # requests one server handles at once
            "timeout": 600,
        },
        "cli": {                      # one process per call; reloads the model every time
            # Placeholders: {model} {accelerator} {context_tokens} {prompt} {prompt_file}
            # `litert-lm run` takes the prompt only as --prompt, so prompt_via is "arg".
            "command": ["litert-lm", "run", "{model}", "--prompt", "{prompt}",
                        "--max-num-tokens", "{context_tokens}", "--cache", "disk"],
            "prompt_via": "arg",      # stdin | file ({prompt_file}) | arg ({prompt})
            "output_regex": "",       # keep only this match (group 1 if present) from stdout
            "timeout": 600,
        },
    },
    "ollama": {                       # [model].backend = "ollama": a running Ollama server
        "base_url": "http://127.0.0.1:11434",
        "model": "",                  # e.g. "qwen3:0.6b"; empty = [model].path, else the first listed
        "keep_alive": "",             # how long Ollama keeps the model loaded, e.g. "30m"; empty = its default
        "think": "",                  # true | false: reasoning models; empty = Ollama's default
        "max_concurrent": 1,          # requests at once (match OLLAMA_NUM_PARALLEL)
        "timeout": 600,
    },
    "resources": {
        "manage_ram": True,           # false: no RAM checks, exactly one model instance
        "reserve_ram_mb": 1024,       # RAM left for the OS and harness
        "overhead_factor": 1.25,      # runtime overhead over the model file size
        "kv_cache_mb": 0,             # 0 = estimate from context_tokens
        "model_mb": 0,                # model size when [model].path is an ID, not a file
        "max_instances": 4,
    },
    "context": {
        "refresh_threshold": 0.80,    # auto-refresh when the window passes this fraction
        "auto_refresh": True,
        "keep_recent_turns": 4,       # turns kept verbatim on refresh
        "auto_retrieve": True,        # pull RAG snippets relevant to each prompt
        "retrieve_k": 3,
        "retrieve_budget": 0.20,      # max fraction of the window for retrieved snippets
        "summary_budget": 0.10,
    },
    "tools": {
        "auto": True,                 # model may call tools on its own
        "max_steps": 6,               # tool calls per user turn
        "max_result_chars": 3000,
        "extra_dirs": [],
    },
    "plan": {
        "default_mode": "fresh",      # fresh | continuous
        "parallel": False,            # run independent tasks on several instances
    },
    "recipe": {
        "library": True,              # code mode: keep each section in the code library, and reuse from it
        "reuse_similarity": 0.85,     # a section this alike (shared words) to a snippet's reuses it, no model call
        "library_k": 3,               # similar snippets shown to the model, which may reuse one
    },
    "research": {
        "k": 6,                       # excerpts found per question
        "excerpt_budget": 0.5,        # max fraction of the window for the excerpts
        "attached_budget": 0.2,       # max fraction for attached answers (CAG)
    },
    "rag": {
        # File types /index accepts, in files and whole folders.
        "text_suffixes": [".txt", ".md", ".py", ".sh"],
        "pdf_suffixes": [".pdf"],
        "image_suffixes": [".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"],
        "max_text_mb": 2,
        "max_ocr_mb": 50,             # PDFs and images
        # Each session's archive of refreshed turns is an index; drop those not used for
        # this many days, and those whose session is gone (0 = keep them all).
        "session_index_days": 30,
        "ocr": {
            # PDFs: auto = use the PDF's own text where a page has it, OCR the others;
            # always = OCR every page.
            "pdf_mode": "auto",
            "min_page_chars": 40,     # a page with less text than this is OCR'd
            "lang": "eng",            # tesseract language(s), e.g. "eng+por"
            "dpi": 200,
            # Commands from poppler (pdftotext, pdftoppm) and tesseract.
            # Placeholders: {input} {output} {page} {lang} {dpi}
            "pdf_text": ["pdftotext", "-layout", "-enc", "UTF-8", "{input}", "-"],
            "pdf_render": ["pdftoppm", "-r", "{dpi}", "-f", "{page}", "-l", "{page}", "-png", "{input}", "{output}"],
            "image": ["tesseract", "{input}", "stdout", "-l", "{lang}"],
            "timeout": 300,           # per command
        },
        "embed": {
            # Hybrid search: keywords (BM25) plus meaning (embeddings), ranked together.
            # auto = fastembed if installed, else the model's server, else keywords only
            # | none = keywords only | server = OpenAI-compatible /v1/embeddings
            # | fastembed = in-process (pip install fastembed)
            "backend": "auto",
            "base_url": "",           # server: empty = [litert.server].base_url
            "model": "",              # server: embedding model ID
            "api_key": "",
            "fastembed_model": "BAAI/bge-small-en-v1.5",
            "query_prefix": "",       # some models want one, e.g. "search_query: " (nomic)
            "doc_prefix": "",         # e.g. "search_document: "
            "min_similarity": 0,      # ignore chunks less similar than this (0 = auto: 0.5 for bge, else 0.3)
            "similarity_margin": 0.15,  # and those this far below the best match
            "batch": 32,
            "timeout": 60,
            "ram_mb": 0,              # fastembed: RAM kept free for it (0 = 300)
        },
    },
    "models": {
        # Model files. Empty: models/ in the SmallHarness checkout, else the user data folder.
        "dir": "",
        "hf_endpoint": "https://huggingface.co",
        "hf_token": "",               # for gated models; the HF_TOKEN environment variable is preferred
        "auto_import": True,          # server mode: import model files into `litert-lm`'s store
        "timeout": 60,                # seconds without data before a download gives up
    },
    "ui": {
        "color": True,
        "banner": "first",            # ASCII title: first (first run only) | always | never
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def is_secret(key: str) -> bool:
    """Settings never shown in full: API keys and tokens."""
    return key.endswith(("api_key", "token"))


def flatten(d: dict, prefix: str = "") -> dict[str, Any]:
    """{"model": {"path": "x"}} -> {"model.path": "x"}"""
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def changed_keys(old: dict, new: dict) -> set[str]:
    a, b = flatten(old), flatten(new)
    return {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}


def parse_value(text: str) -> Any:
    """A TOML value (`4096`, `true`, `["a", "b"]`, `"x"`); anything else is a plain string."""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


@dataclass
class Config:
    workspace: Path
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    files: list[Path] = field(default_factory=list)     # config files that were read
    _context: tuple | None = field(default=None, repr=False, compare=False)
    explicit: Path | None = None
    overrides: dict = field(default_factory=dict)       # from command-line flags
    trust_run: bool = False                              # --trust: trust the workspace for this run only
    # Code-running settings the workspace's smallharness.toml asked for, as last read.
    _ws_settings: dict = field(default_factory=dict, repr=False, compare=False)
    untrusted: trust.WorkspaceCode | None = None         # workspace code withheld (not trusted)
    _state: Any = field(default=None, repr=False, compare=False)

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data[section]

    @property
    def state(self):
        """The workspace's `.smallharness/` folder (a state.StateDir)."""
        if self._state is None:
            from .state import StateDir
            self._state = StateDir(self.workspace / ".smallharness")
        return self._state

    @property
    def context(self):
        """The context window in use (a resources.ContextChoice). Chosen once per model
        and setting, so it stays stable while RAM use changes during a run."""
        from .engines import engine
        m, res, o = self["model"], self["resources"], self["ollama"]
        key = (m["backend"], m["path"], m["context_tokens"], m.get("auto_context_max"),
               res.get("manage_ram"), res.get("model_mb"), o.get("model"), o.get("base_url"))
        if self._context is None or self._context[0] != key:
            choice, keep = engine(self).context_choice()
            if not keep:
                return choice
            self._context = (key, choice)
        return self._context[1]

    @property
    def context_tokens(self) -> int:
        return self.context.tokens

    @property
    def model_name(self) -> str:
        from .engines import engine
        return engine(self).model_name()

    def with_values(self, values: dict[str, Any]) -> dict:
        """A copy of the data with each `section.key = value` set. Only known keys can be set."""
        known = flatten(DEFAULTS)
        data = copy.deepcopy(self.data)
        for key, value in values.items():
            if key not in known:
                raise KeyError(f"unknown setting {key!r}")
            *path, last = key.split(".")
            node = data
            for part in path:
                node = node[part]
            node[last] = value
        data["model"]["path"] = _normalize_model_path(data)
        return data

    def read_files(self) -> tuple[dict, list[Path]]:
        """Defaults + config files + command-line overrides, as at startup."""
        data, files = copy.deepcopy(DEFAULTS), []
        candidates = [self.explicit] if self.explicit else [
            Path.home() / ".config" / "smallharness" / "config.toml",
            self.workspace / "smallharness.toml",
        ]
        ws_file = None if self.explicit else self.workspace / "smallharness.toml"
        self._ws_settings = {}
        for p in candidates:
            if p and p.is_file():
                with open(p, "rb") as f:
                    over = tomllib.load(f)
                if p == ws_file:
                    over = self._withhold_untrusted(over)
                data = deep_merge(data, over)
                files.append(p)
        if ws_file is None or not ws_file.is_file():
            self.untrusted = None if self.workspace_trusted() else self.workspace_code()
        if self.overrides:
            data = deep_merge(data, self.overrides)
        data["model"]["path"] = _normalize_model_path(data)
        return data, files

    # --------------------------------------------------------------- trust
    def workspace_code(self) -> trust.WorkspaceCode:
        """The code this workspace asks the harness to run, as it is on disk now."""
        return trust.WorkspaceCode(self.workspace, trust.tool_files(self.workspace), dict(self._ws_settings))

    def workspace_trusted(self) -> bool:
        """Checked again whenever workspace code is about to be used, so a tool file
        added or changed after trust was given is not run without asking."""
        return self.trust_run or trust.is_trusted(self.workspace_code())

    def _withhold_untrusted(self, ws: dict) -> dict:
        """Drop the workspace file's code-running settings unless the workspace is trusted."""
        defaults = flatten(DEFAULTS)
        self._ws_settings = {k: v for k, v in flatten(ws).items()
                             if k in trust.CODE_KEYS and v != defaults[k]}
        if self.workspace_trusted():
            self.untrusted = None
            return ws
        self.untrusted = self.workspace_code()
        ws = copy.deepcopy(ws)
        for key in self._ws_settings:
            *path, last = key.split(".")
            node = ws
            for part in path:
                node = node[part]
            del node[last]
        return ws

    def grant_trust(self) -> None:
        """Trust the workspace's code as it is now, and re-read the config to use it."""
        trust.grant(self.workspace_code())
        self.data, self.files = self.read_files()

    @classmethod
    def load(cls, workspace: Path, explicit: Path | None = None, overrides: dict | None = None,
             trust_run: bool = False) -> "Config":
        cfg = cls(workspace=workspace.resolve(), explicit=explicit,
                  overrides={k: v for k, v in (overrides or {}).items() if v}, trust_run=trust_run)
        cfg.data, cfg.files = cfg.read_files()
        return cfg


def _normalize_model_path(data: dict) -> str:
    """A file path, a short name for a file in the model folder, or a server model ID."""
    from .models import resolve_model_path
    return resolve_model_path(data["model"]["path"], data["models"])
