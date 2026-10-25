"""The engines that run models: LiteRT-LM (model files in the model folder, run in
process, by command, or by `litert-lm serve`) and Ollama (its own model store).

Everything that depends on which engine is configured lives here: the model's name, the
server it is reached at, its context window, listing, downloading and deleting models,
and turning `/model X` into settings. The rest of the harness asks `engine(cfg)`
instead of testing [model].backend; the terminal and the web interface share these
operations and only present the results."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import errors
from .config import parse_value

Progress = Callable[[int, "int | None"], None]


class ModelError(errors.HarnessError, RuntimeError):
    """A model operation that cannot be done; the message says why."""


class NotFound(ModelError, errors.NotFound):
    pass


class InUse(ModelError, errors.Conflict):
    pass


@dataclass
class ModelRow:
    name: str                      # what to load it by
    size: int                      # bytes
    max_context: int | None
    about: str                     # source, or parameters and quantization
    active: bool                   # the model the settings point at


@dataclass
class Available:
    name: str
    size_gb: float
    note: str
    gated: bool


@dataclass
class Listing:
    where: str
    models: list[ModelRow] = field(default_factory=list)
    available: list[Available] = field(default_factory=list)
    error: str = ""                # the engine could not be asked


@dataclass
class Deletion:
    """What a delete would remove, to show before asking."""
    name: str                      # as the engine names it
    items: list[str]               # file names, or the Ollama model
    size: int                      # bytes, 0 if unknown
    where: str
    active: bool
    _target: object = None


@dataclass
class Deleted:
    freed: int = 0
    note: str = ""                 # e.g. a LiteRT-LM store link that still holds the file
    items: list[str] = field(default_factory=list)


@dataclass
class PullPlan:
    """What a download would fetch, before it starts: shown, then RAM-checked."""
    spec: str
    what: str                      # source -> destination
    size: int                      # bytes, 0 if not known in advance
    warning: str = ""              # from the RAM check
    wont_run: bool = False         # too big for this machine at all
    _remote: object = None


@dataclass
class Pulled:
    name: str                      # load it by this
    already: bool = False
    detail: str = ""               # how it was checked, resumed, ...
    warning: str = ""              # RAM warning known only after the download


# `/model` options -> settings
MODEL_OPTIONS = {"ctx": "model.context_tokens", "out": "model.max_output_tokens", "template": "model.template",
                 "name": "model.name", "path": "model.path", "id": "litert.server.model",
                 "mb": "resources.model_mb"}


class Engine:
    kind = ""

    def __init__(self, cfg):
        self.cfg = cfg

    # ----------------------------------------------------------- identity
    def model_name(self) -> str:
        raise NotImplementedError

    def server_url(self) -> str:
        """The configured server that also serves embeddings, or ''."""
        return ""

    def context_choice(self):
        """(resources.ContextChoice, keep): `keep` is False for a stand-in that should be
        worked out again next time (the engine did not answer)."""
        raise NotImplementedError

    def context_note(self, backend_mode: str = "") -> str:
        """How this engine applies the window, for `/context`."""
        return ""

    def default_embed_model(self) -> str:
        """The embedding model asked for when [rag.embed] names none (backend auto)."""
        return ""

    def listing(self) -> Listing:
        raise NotImplementedError

    def plan_delete(self, name: str, in_use: bool = False) -> Deletion:
        """What deleting `name` removes. NotFound if there is no such model; InUse if it
        is the model the settings point at and `in_use` (it is loaded right now)."""
        raise NotImplementedError

    def delete(self, d: Deletion) -> Deleted:
        raise NotImplementedError

    def plan_pull(self, spec: str) -> PullPlan:
        raise NotImplementedError

    def pull(self, plan: PullPlan, progress: Progress | None = None, force: bool = False) -> Pulled:
        raise NotImplementedError

    def _check_in_use(self, d: Deletion, in_use: bool) -> Deletion:
        if d.active and in_use:
            raise InUse(f"{d.items[0]} is the model in use; switch to another one first")
        return d

    # ----------------------------------------------------------- switching
    def switch_values(self, words: list[str]) -> tuple[dict, list[str]]:
        """`/model TARGET [ctx=N|auto] [out=N] …` -> (settings, warnings). Raises errors.Invalid."""
        values: dict = {}
        target = words[0] if words and "=" not in words[0] else None
        for w in words[1:] if target else words:
            key, eq, val = w.partition("=")
            if not eq or key not in MODEL_OPTIONS:
                raise errors.Invalid(f"unknown option {w!r}; options: {', '.join(k + '=' for k in MODEL_OPTIONS)}")
            values[MODEL_OPTIONS[key]] = parse_value(val)
        warnings: list[str] = []
        if target:
            self._target_values(target, values, warnings)
        ctx = values.get("model.context_tokens")
        if "model.context_tokens" in values and ctx != "auto" and not isinstance(ctx, int):
            raise errors.Invalid("ctx must be a whole number or auto")
        for key in ("model.max_output_tokens", "resources.model_mb"):
            if key in values and not isinstance(values[key], int):
                raise errors.Invalid(f"{key} must be a whole number")
        if values.get("model.template", "plain") not in ("plain", "gemma"):
            raise errors.Invalid("template must be plain or gemma")
        return values, warnings

    def _target_values(self, target: str, values: dict, warnings: list[str]) -> None:
        raise NotImplementedError


# ------------------------------------------------------------------ LiteRT
class LiteRTEngine(Engine):
    """Model files in the model folder (also the store for the mock backend)."""
    kind = "litert"

    def model_name(self) -> str:
        m = self.cfg["model"]
        return m["name"] or (Path(m["path"]).stem if m["path"] else m["backend"])

    def server_url(self) -> str:
        return self.cfg["litert"]["server"].get("base_url", "")

    def server_mode(self) -> bool:
        from .backends.litert import resolve_mode
        if self.cfg["model"]["backend"] != "litert":
            return False
        try:
            return resolve_mode(self.cfg["litert"]) == "server"
        except RuntimeError:
            return False

    def context_choice(self):
        from .resources import choose_context
        m = self.cfg["model"]
        return choose_context(m, self.cfg["resources"], m["path"]), True

    def context_note(self, backend_mode: str = "") -> str:
        if backend_mode == "server":
            return ("server mode: `litert-lm serve` sizes its own window (its config.json); "
                    "the harness keeps prompts within the size above")
        return ""

    def _dir(self) -> tuple[Path, str]:
        from . import models
        return models.models_dir(self.cfg["models"])

    def _active(self, path: Path) -> bool:
        active = self.cfg["model"]["path"]
        return bool(active) and Path(active) == path

    def listing(self) -> Listing:
        from . import models
        d, why = self._dir()
        local = models.list_local(d)
        have = {m.path.name for m in local}
        return Listing(f"{d} ({why})",
                       [ModelRow(m.name, m.size, m.max_context, m.source, self._active(m.path)) for m in local],
                       [Available(e.name, e.size_gb, e.note, e.gated)
                        for e in models.CATALOG.values() if e.file not in have])

    def plan_delete(self, name: str, in_use: bool = False) -> Deletion:
        from . import models
        d = self._dir()[0]
        files = models.find_deletable(name, d)
        if not files:
            raise NotFound(f"no model {name!r} in {d}")
        return self._check_in_use(Deletion(name, [p.name for p in files], sum(p.stat().st_size for p in files),
                                           str(d), any(self._active(p) for p in files)), in_use)

    def delete(self, dl: Deletion) -> Deleted:
        from . import models
        try:
            got = models.delete(dl.name, Path(dl.where))
        except OSError as e:
            raise ModelError(f"could not delete: {e}") from None
        note = (f"the LiteRT-LM store still links the same file as {got.store_id!r}; "
                f"`litert-lm delete {got.store_id}` frees the rest") if got.store_id else ""
        return Deleted(got.freed, note, [p.name for p in got.files])

    def plan_pull(self, spec: str) -> PullPlan:
        from . import models
        try:
            src, _ = models.parse_spec(spec)
            remote = models.inspect(src, self.cfg["models"])
        except models.PullError as e:
            raise ModelError(str(e)) from None
        warning, wont_run = ram_warning(self.cfg, remote.size) if remote.size else ("", False)
        return PullPlan(spec, f"{src.repo}/{src.file} → {self._dir()[0]}", remote.size or 0,
                        warning, wont_run, remote)

    def pull(self, plan: PullPlan, progress: Progress | None = None, force: bool = False) -> Pulled:
        from . import models
        try:
            got = models.pull(plan.spec, self.cfg["models"], progress, force=force, remote=plan._remote)
        except models.PullError as e:
            raise ModelError(str(e)) from None
        if got.already:
            return Pulled(short_name(got.path), True, f"already downloaded: {got.path}")
        how = "SHA-256 verified" if got.verified else "no checksum published; size checked"
        resumed = f", resumed at {got.resumed_from / 1e9:.2f} GB" if got.resumed_from else ""
        return Pulled(short_name(got.path), False, f"{got.path.name} ({got.size / 1e9:.2f} GB, {how}{resumed})")

    def _target_values(self, target: str, values: dict, warnings: list[str]) -> None:
        """In server mode the target is the model ID the server serves (unless it is a
        file); otherwise it is the model file, by path or short name."""
        from .models import resolve_model_path, server_id
        server = self.server_mode()
        found = resolve_model_path(target, self.cfg["models"])
        if Path(found).is_file():
            target = found
            if server and self.cfg["models"]["auto_import"]:
                values.setdefault("litert.server.model", server_id(found))
        if server and not Path(target).expanduser().is_file():
            values["litert.server.model"] = target
            values.setdefault("model.name", target)
            return
        values["model.path"] = target
        values.setdefault("model.name", "")          # show the new file's name
        if server and "litert.server.model" not in values:
            warnings.append(f"server mode: {target} is used for the RAM estimate only; the server's model is "
                            "chosen with id=… (or set [models].auto_import to import the file)")


def short_name(path: Path) -> str:
    """The short name to load a model file by."""
    from .models import CATALOG
    return next((e.name for e in CATALOG.values() if e.file == path.name), path.stem)


# ------------------------------------------------------------------ Ollama
class OllamaEngine(Engine):
    """Ollama keeps its own models, pulled by tag such as `qwen3:0.6b`."""
    kind = "ollama"

    def _client(self):
        from .backends.ollama import Client
        return Client(self.cfg["ollama"])

    def model(self) -> str:
        from .backends.ollama import ollama_model
        return ollama_model(self.cfg)

    def model_name(self) -> str:
        return self.cfg["model"]["name"] or self.model() or "ollama"

    def server_url(self) -> str:
        return self.cfg["ollama"]["base_url"]   # Ollama also serves /v1/embeddings

    def context_choice(self):
        """The maximum comes from Ollama; when Ollama runs on this machine, the model's
        size is used to fit the window in free RAM."""
        from .backends.ollama import is_local, model_facts
        from .resources import FALLBACK_CONTEXT, ContextChoice, choose_context
        m, res, o = self.cfg["model"], self.cfg["resources"], self.cfg["ollama"]
        facts = model_facts(o, self.model())
        if facts is None:
            ctx = m["context_tokens"]
            return ContextChoice(ctx if isinstance(ctx, int) and ctx > 0 else FALLBACK_CONTEXT,
                                 "Ollama did not answer; context size not checked"), False
        if is_local(o["base_url"]) and not res.get("model_mb"):
            res = {**res, "model_mb": facts.size // 2**20}
        elif not is_local(o["base_url"]):
            res = {**res, "manage_ram": False}   # another machine's RAM
        return choose_context(m, res, "", model_max=facts.max_context, source="Ollama"), True

    def context_note(self, backend_mode: str = "") -> str:
        return "Ollama: the harness sends this size as num_ctx with every request"

    def default_embed_model(self) -> str:
        return "nomic-embed-text"   # `ollama pull nomic-embed-text`

    def _active(self, name: str) -> bool:
        from .backends.ollama import matches
        active = self.model()
        return bool(active) and matches(active, name)

    def listing(self) -> Listing:
        from .backends.ollama import OllamaError
        client = self._client()
        try:
            found = client.list()
        except OllamaError as e:
            return Listing(f"Ollama at {client.base}", error=str(e))
        return Listing(f"Ollama at {client.base}",
                       [ModelRow(m.name, m.size, m.max_context,
                                 " ".join(x for x in (m.parameters, m.quantization) if x), self._active(m.name))
                        for m in found])

    def plan_delete(self, name: str, in_use: bool = False) -> Deletion:
        from .backends.ollama import OllamaError, matches
        client = self._client()
        try:
            names = client.names()
        except OllamaError as e:
            raise ModelError(str(e)) from None
        match = next((n for n in names if matches(name, n)), None)
        if not match:
            raise NotFound(f"Ollama has no model {name!r}")
        return self._check_in_use(Deletion(match, [match], 0, f"Ollama at {client.base}", self._active(match)), in_use)

    def delete(self, dl: Deletion) -> Deleted:
        from .backends.ollama import OllamaError
        try:
            self._client().delete(dl.name)
        except OllamaError as e:
            raise ModelError(f"could not delete: {e}") from None
        return Deleted(items=[dl.name])

    def plan_pull(self, spec: str) -> PullPlan:
        # Ollama does not say the size before it starts, so the RAM check comes after.
        return PullPlan(spec, f"{spec} → Ollama at {self._client().base}", 0)

    def pull(self, plan: PullPlan, progress: Progress | None = None, force: bool = False) -> Pulled:
        from .backends.ollama import OllamaError, is_local
        client = self._client()
        try:
            client.pull(plan.spec, progress)
        except OllamaError as e:
            raise ModelError(str(e)) from None
        warning = ""
        if is_local(client.base):
            try:
                size = client.show(plan.spec).size
            except OllamaError:
                size = 0
            warning = ram_warning(self.cfg, size)[0] if size else ""
        return Pulled(plan.spec, False, f"{plan.spec} is in Ollama", warning)

    def _target_values(self, target: str, values: dict, warnings: list[str]) -> None:
        values["ollama.model"] = target              # an Ollama name, e.g. qwen3:0.6b
        values.setdefault("model.name", "")


def engine(cfg) -> Engine:
    return OllamaEngine(cfg) if cfg["model"]["backend"] == "ollama" else LiteRTEngine(cfg)


def ram_warning(cfg, size: int) -> tuple[str, bool]:
    """(warning, will_not_run) for a model of `size` bytes, from the RAM check."""
    from .resources import capacity
    res = cfg["resources"]
    if not res.get("manage_ram", True):
        return "", False
    try:
        rep = capacity("", cfg["model"].get("auto_context_max") or 4096, {**res, "model_mb": size // 2**20})
    except Exception:  # RAM unreadable here: no basis for a warning
        return "", False
    if rep.fits:
        return "", False
    if rep.footprint_mb > rep.ram.total_mb - res.get("reserve_ram_mb", 1024):
        return (f"this model needs about {rep.footprint_mb} MB of RAM and this machine has "
                f"{rep.ram.total_mb} MB in total: it will not run here"), True
    return (f"this model needs about {rep.footprint_mb} MB of RAM; {max(rep.usable_mb, 0)} MB is free "
            "now, so close other programs before loading it"), False


def make_embedder_for(cfg, server_url=None):
    """The embedder [rag.embed] asks for, with this engine's server and default model."""
    from .context.embed import make_embedder
    eng = engine(cfg)
    return make_embedder(cfg["rag"]["embed"], server_url or eng.server_url, eng.default_embed_model())
