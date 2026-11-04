"""Speech: speech to text (Whisper, Vosk) and text to speech (Piper, Bark), run on this
machine. Each engine is an optional package; its models are pulled like the language
models, into `<models folder>/speech/`.

| engine  | does | package (pip)    | models (`smallharness pull …`)                        |
|---------|------|------------------|-------------------------------------------------------|
| whisper | STT  | faster-whisper   | whisper:tiny|base|small|medium|large-v3|distil-large-v3 (Hugging Face, Systran) |
| vosk    | STT  | vosk             | vosk:NAME from alphacephei.com/vosk/models (zip, MD5 checked) |
| piper   | TTS  | piper-tts        | piper:VOICE, e.g. piper:en_US-lessac-medium (Hugging Face, rhasspy/piper-voices) |
| bark    | TTS  | suno-bark        | bark, or bark:small (its own download, into its cache) |

Audio in is WAV (16-bit PCM; the web page records it so) or, for Whisper, any format
it can decode. Audio out is WAV. Loaded models are kept for the next call."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import wave
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .errors import HarnessError, Invalid, NotFound

# engine -> (stt | tts, module to import, pip package)
ENGINES: dict[str, tuple[str, str, str]] = {
    "whisper": ("stt", "faster_whisper", "faster-whisper"),
    "vosk": ("stt", "vosk", "vosk"),
    "piper": ("tts", "piper", "piper-tts"),
    "bark": ("tts", "bark", "suno-bark"),
}
WHISPER = {   # size -> (Hugging Face repo, MB)
    "tiny": ("Systran/faster-whisper-tiny", 75), "base": ("Systran/faster-whisper-base", 145),
    "small": ("Systran/faster-whisper-small", 484), "medium": ("Systran/faster-whisper-medium", 1530),
    "large-v3": ("Systran/faster-whisper-large-v3", 3090),
    "distil-large-v3": ("Systran/faster-distil-whisper-large-v3", 1510),
}
_WHISPER_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
_WHISPER_V3_FILES = ["config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"]
PIPER_REPO = "rhasspy/piper-voices"
PIPER_VOICES = {   # suggested; any voice from huggingface.co/rhasspy/piper-voices works
    "en_US-lessac-medium": 63, "en_US-amy-medium": 63, "en_GB-alan-medium": 63, "pt_PT-tugão-medium": 63,
    "pt_BR-faber-medium": 63, "es_ES-davefx-medium": 63, "fr_FR-siwis-medium": 63, "de_DE-thorsten-medium": 63,
}
VOSK_LIST = "https://alphacephei.com/vosk/models/model-list.json"
VOSK_SUGGESTED = {"vosk-model-small-en-us-0.15": 40, "vosk-model-en-us-0.22": 1800, "vosk-model-small-pt-0.3": 31,
                  "vosk-model-small-es-0.42": 39, "vosk-model-small-fr-0.22": 41, "vosk-model-small-de-0.15": 45}
_PIPER_NAME = re.compile(r"^([a-z]{2,3})_([A-Z]{2})-([^/\\\s]+)-(x_low|low|medium|high)$")
_VOSK_NAME = re.compile(r"^vosk-model-[\w.-]+$")
MAX_TTS_CHARS = 4000

Progress = Callable[[int, "int | None"], None]


class SpeechError(HarnessError):
    pass


@dataclass
class Item:
    """A speech model, pulled or available to pull."""
    name: str            # what `pull` takes: whisper:base, piper:en_US-lessac-medium, …
    engine: str
    size_mb: int = 0
    path: str = ""       # where it is, once pulled
    note: str = ""

    @property
    def kind(self) -> str:
        return ENGINES[self.engine][0]

    def to_dict(self) -> dict:
        return {"name": self.name, "engine": self.engine, "kind": self.kind, "size_mb": self.size_mb,
                "path": self.path, "note": self.note}


# ------------------------------------------------------------------ where
def speech_dir(cfg) -> Path:
    from .models import models_dir
    return models_dir(cfg["models"])[0] / "speech"


def installed(engine: str) -> bool:
    return importlib.util.find_spec(ENGINES[engine][1]) is not None


def _bark_cache() -> Path:
    base = os.getenv("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "suno" / "bark_v0"


def local(cfg) -> list[Item]:
    """The speech models pulled so far."""
    d, out = speech_dir(cfg), []
    for size in WHISPER:
        p = d / "whisper" / size
        if (p / "model.bin").is_file():
            out.append(Item(f"whisper:{size}", "whisper", _mb(p), str(p)))
    if (d / "vosk").is_dir():
        for p in sorted((d / "vosk").iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                out.append(Item(f"vosk:{p.name}", "vosk", _mb(p), str(p)))
    if (d / "piper").is_dir():
        for p in sorted((d / "piper").glob("*.onnx")):
            if p.with_suffix(".onnx.json").is_file():
                out.append(Item(f"piper:{p.stem}", "piper", _mb(p), str(p)))
    for small, files in ((False, ("text_2.pt", "coarse_2.pt", "fine_2.pt")), (True, ("text.pt", "coarse.pt", "fine.pt"))):
        if all((_bark_cache() / f).is_file() for f in files):
            out.append(Item("bark:small" if small else "bark", "bark",
                            sum(int((_bark_cache() / f).stat().st_size) for f in files) // 1_000_000, str(_bark_cache())))
    return out


def available() -> list[Item]:
    """Suggested models to pull."""
    return ([Item(f"whisper:{s}", "whisper", mb, note="multilingual") for s, (_, mb) in WHISPER.items()]
            + [Item(f"vosk:{n}", "vosk", mb, note="any name from alphacephei.com/vosk/models") for n, mb in VOSK_SUGGESTED.items()]
            + [Item(f"piper:{n}", "piper", mb, note="any voice from rhasspy/piper-voices") for n, mb in PIPER_VOICES.items()]
            + [Item("bark", "bark", 5000, note="needs PyTorch; downloads into its own cache"),
               Item("bark:small", "bark", 1300, note="smaller, faster Bark models")])


def status(cfg) -> dict:
    s = cfg["speech"]
    pulled = local(cfg)
    return {"settings": dict(s), "dir": str(speech_dir(cfg)),
            "engines": {e: {"kind": k, "installed": installed(e), "pip": pip} for e, (k, _, pip) in ENGINES.items()},
            "local": [i.to_dict() for i in pulled],
            "available": [i.to_dict() for i in available() if i.name not in {p.name for p in pulled}],
            "stt_ready": _ready(cfg, "stt"), "tts_ready": _ready(cfg, "tts")}


def _ready(cfg, kind: str) -> str:
    """"" if the chosen engine can run now, else why not."""
    engine = cfg["speech"][kind]
    if engine in ("", "none"):
        return f"no {kind.upper()} engine chosen ([speech].{kind})"
    try:
        _check(engine, kind)
        _model_path(cfg, engine)
    except HarnessError as e:
        return str(e)
    return ""


def _mb(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size // 1_000_000
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) // 1_000_000


def _check(engine: str, kind: str | None = None) -> None:
    if engine not in ENGINES:
        raise Invalid(f"no speech engine {engine!r}; engines: {', '.join(ENGINES)}")
    if kind and ENGINES[engine][0] != kind:
        raise Invalid(f"{engine} does {ENGINES[engine][0].upper()}, not {kind.upper()}")
    if not installed(engine):
        raise SpeechError(f"{engine} is not installed: pip install {ENGINES[engine][2]}")


def _model_path(cfg, engine: str, model: str | None = None) -> Path:
    """The model the settings (or `model`) name, as a path; it must be pulled."""
    s = cfg["speech"]
    model = model or {"whisper": s["whisper_model"], "vosk": s["vosk_model"], "piper": s["piper_voice"],
                      "bark": ""}[engine]
    if engine == "bark":
        return _bark_cache()
    as_path = Path(model).expanduser()
    if as_path.is_absolute() and as_path.exists():
        return as_path
    d = speech_dir(cfg)
    p = {"whisper": d / "whisper" / model, "vosk": d / "vosk" / model, "piper": d / "piper" / f"{model}.onnx"}[engine]
    if not p.exists():
        raise NotFound(f"{engine} model {model!r} is not pulled: smallharness pull {engine}:{model}")
    return p


# ------------------------------------------------------------------- pull
def parse(spec: str) -> tuple[str, str] | None:
    """`whisper:base` -> ("whisper", "base"); None if `spec` is not a speech model."""
    engine, _, name = spec.strip().partition(":")
    if engine.lower() not in ENGINES:
        return None
    return engine.lower(), name.strip()


def pull(cfg, spec: str, progress: Progress | None = None, force: bool = False) -> Item:
    parsed = parse(spec)
    if not parsed:
        raise Invalid(f"not a speech model: {spec!r} (whisper:SIZE, vosk:NAME, piper:VOICE, bark)")
    engine, name = parsed
    d = speech_dir(cfg)
    if engine == "whisper":
        name = name or "base"
        if name not in WHISPER:
            raise Invalid(f"no Whisper size {name!r}; sizes: {', '.join(WHISPER)}")
        return _pull_hf_files(cfg, WHISPER[name][0], _WHISPER_V3_FILES if "large-v3" in name else _WHISPER_FILES,
                              d / "whisper" / name, f"whisper:{name}", "whisper", progress, force)
    if engine == "piper":
        m = _PIPER_NAME.match(name or "")
        if not m:
            raise Invalid("a Piper voice is named like en_US-lessac-medium (see huggingface.co/rhasspy/piper-voices)")
        lang, region, voice, quality = m.groups()
        folder = f"{lang}/{lang}_{region}/{voice}/{quality}"
        item = _pull_hf_files(cfg, PIPER_REPO, [f"{folder}/{name}.onnx", f"{folder}/{name}.onnx.json"],
                              d / "piper", f"piper:{name}", "piper", progress, force, flat=True)
        item.path = str(d / "piper" / f"{name}.onnx")
        return item
    if engine == "vosk":
        return _pull_vosk(cfg, name or "vosk-model-small-en-us-0.15", d / "vosk", progress, force)
    return _pull_bark(name == "small")


def _pull_hf_files(cfg, repo: str, files: list[str], dest: Path, label: str, engine: str,
                   progress: Progress | None, force: bool, flat: bool = False) -> Item:
    from .models import PullError, Source, download, inspect
    mcfg = cfg["models"]
    try:
        remotes = [(f, inspect(Source(repo, f), mcfg)) for f in files]
        total = sum(r.size or 0 for _, r in remotes) or None
        done_before = 0
        for f, remote in remotes:
            target = dest / (Path(f).name if flat else f)
            got = download(Source(repo, f), target, mcfg,
                           (lambda d, t, base=done_before: progress(base + d, total)) if progress else None,
                           force, remote)
            done_before += got.size
    except PullError as e:
        raise SpeechError(str(e)) from None
    if progress:
        progress(done_before, total)
    return Item(label, engine, done_before // 1_000_000, str(dest))


def _pull_vosk(cfg, name: str, dest_root: Path, progress: Progress | None, force: bool) -> Item:
    if not _VOSK_NAME.match(name):
        raise Invalid("a Vosk model is named like vosk-model-small-en-us-0.15 (see alphacephei.com/vosk/models)")
    dest = dest_root / name
    if dest.is_dir() and not force:
        return Item(f"vosk:{name}", "vosk", _mb(dest), str(dest), note="already pulled")
    timeout = cfg["models"].get("timeout", 60)
    try:
        with urllib.request.urlopen(urllib.request.Request(VOSK_LIST, headers={"User-Agent": "smallharness"}),
                                    timeout=timeout) as r:
            listing = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SpeechError(f"the Vosk model list could not be read: {getattr(e, 'reason', e)}") from None
    entry = next((m for m in listing if m.get("name") == name), None)
    if not entry:
        raise NotFound(f"no Vosk model {name!r}; see https://alphacephei.com/vosk/models")
    url = entry.get("url", "")
    if not url.startswith("https://alphacephei.com/"):
        raise SpeechError(f"unexpected download address for {name}: {url}")
    dest_root.mkdir(parents=True, exist_ok=True)
    part = dest_root / f".{name}.zip.part"
    size = int(entry.get("size") or 0) or None
    md5, done = hashlib.md5(), 0
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "smallharness"}),
                                    timeout=timeout) as r, open(part, "wb") as f:
            while block := r.read(1 << 20):
                f.write(block)
                md5.update(block)
                done += len(block)
                if progress:
                    progress(done, size)
    except (urllib.error.URLError, OSError) as e:
        part.unlink(missing_ok=True)
        raise SpeechError(f"download interrupted ({getattr(e, 'reason', e)}); pull again") from None
    if entry.get("md5") and md5.hexdigest() != entry["md5"]:
        part.unlink(missing_ok=True)
        raise SpeechError("the download does not match its published MD5; it was deleted, pull again")
    tmp = dest_root / f".{name}.unpacking"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(part) as z:
            for member in z.namelist():           # nothing may land outside the folder
                if not (tmp / member).resolve().is_relative_to(tmp.resolve()):
                    raise SpeechError(f"unsafe path in the archive: {member}")
            z.extractall(tmp)
    except zipfile.BadZipFile:
        raise SpeechError("the download is not a zip file") from None
    finally:
        part.unlink(missing_ok=True)
    inner = [p for p in tmp.iterdir() if p.is_dir()]
    root = inner[0] if len(inner) == 1 and not any(p.is_file() for p in tmp.iterdir()) else tmp
    shutil.rmtree(dest, ignore_errors=True)
    root.rename(dest)
    shutil.rmtree(tmp, ignore_errors=True)
    return Item(f"vosk:{name}", "vosk", _mb(dest), str(dest))


def _pull_bark(small: bool) -> Item:
    _check("bark")
    if small:
        os.environ["SUNO_USE_SMALL_MODELS"] = "1"
    from bark import preload_models
    preload_models(text_use_small=small, coarse_use_small=small, fine_use_small=small)
    return Item("bark:small" if small else "bark", "bark", 0, str(_bark_cache()))


def delete(cfg, spec: str) -> Item:
    item = next((i for i in local(cfg) if i.name == spec.strip()), None)
    if not item:
        raise NotFound(f"no pulled speech model {spec!r}")
    p = Path(item.path)
    if item.engine == "bark":
        names = ("text.pt", "coarse.pt", "fine.pt") if item.name == "bark:small" else ("text_2.pt", "coarse_2.pt", "fine_2.pt")
        for n in names:
            (p / n).unlink(missing_ok=True)
        return item
    if not p.resolve().is_relative_to(speech_dir(cfg).resolve()):
        raise Invalid(f"{p} is not in the speech folder; delete it yourself")
    if item.engine == "piper":
        p.unlink(missing_ok=True)
        p.with_suffix(".onnx.json").unlink(missing_ok=True)
    else:
        shutil.rmtree(p)
    _LOADED.pop((item.engine, str(p)), None)
    return item


# ------------------------------------------------------------ run engines
_LOADED: dict[tuple[str, str], object] = {}
_LOCK = threading.Lock()


def _load(engine: str, path: Path, cfg):
    key = (engine, str(path))
    if key not in _LOADED:
        device = cfg["speech"].get("device", "cpu") or "cpu"
        if engine == "whisper":
            from faster_whisper import WhisperModel
            _LOADED[key] = WhisperModel(str(path), device=device, compute_type="int8" if device == "cpu" else "default")
        elif engine == "vosk":
            import vosk
            vosk.SetLogLevel(-1)
            _LOADED[key] = vosk.Model(str(path))
        elif engine == "piper":
            from piper import PiperVoice
            _LOADED[key] = PiperVoice.load(str(path))
    return _LOADED.get(key)


def transcribe(cfg, audio: bytes, engine: str | None = None, model: str | None = None,
               language: str | None = None) -> dict:
    """Speech to text: {"text", "engine", "model", "seconds"}."""
    if not audio:
        raise Invalid("no audio")
    engine = engine or cfg["speech"]["stt"]
    if engine in ("", "none"):
        raise Invalid("no speech-to-text engine: set [speech].stt to whisper or vosk")
    _check(engine, "stt")
    path = _model_path(cfg, engine, model)
    language = (language if language is not None else cfg["speech"].get("language", "")) or None
    start = time.perf_counter()
    with _LOCK:
        m = _load(engine, path, cfg)
        text = _whisper(m, audio, language) if engine == "whisper" else _vosk(m, audio)
    return {"text": text, "engine": engine, "model": path.name, "seconds": round(time.perf_counter() - start, 2)}


def _whisper(model, audio: bytes, language: str | None) -> str:
    # WAV is decoded here, so only other formats need faster-whisper's own decoder (PyAV).
    source = _samples_16k(audio) if audio[:4] == b"RIFF" else io.BytesIO(audio)
    segments, _info = model.transcribe(source, language=language, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def _samples_16k(audio: bytes):
    """A WAV as the float samples at 16 kHz that Whisper takes (numpy comes with it)."""
    import numpy as np
    rate, frames = read_wav(audio)
    x = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if rate != 16000 and len(x):
        n = int(len(x) * 16000 / rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
    return x


def _vosk(model, audio: bytes) -> str:
    import vosk
    rate, frames = read_wav(audio)
    rec = vosk.KaldiRecognizer(model, rate)
    parts = []
    step = 8000                                    # bytes: 4000 frames of 16-bit mono
    for i in range(0, len(frames), step):
        if rec.AcceptWaveform(frames[i:i + step]):
            parts.append(json.loads(rec.Result()).get("text", ""))
    parts.append(json.loads(rec.FinalResult()).get("text", ""))
    return " ".join(p for p in parts if p).strip()


def read_wav(audio: bytes) -> tuple[int, bytes]:
    """(sample rate, 16-bit mono frames) from a WAV file; stereo is mixed down."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            rate, width, channels, frames = w.getframerate(), w.getsampwidth(), w.getnchannels(), w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        raise Invalid("not a WAV file (16-bit PCM); Whisper can also read other formats") from None
    if width != 2:
        raise Invalid(f"the WAV is {8 * width}-bit; Vosk needs 16-bit PCM")
    if channels > 1:
        import array
        a = array.array("h", frames)
        frames = array.array("h", (sum(a[i:i + channels]) // channels for i in range(0, len(a), channels))).tobytes()
    return rate, frames


def speakable(text: str) -> str:
    """What is worth reading aloud: code blocks, links and Markdown marks left out."""
    text = re.sub(r"```.*?```", " (code omitted) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^[#>*\-\s]+", "", text, flags=re.M)
    text = re.sub(r"[*_#|]+", "", text)
    return " ".join(text.split())


def speak(cfg, text: str, engine: str | None = None, voice: str | None = None) -> dict:
    """Text to speech: {"audio": WAV bytes, "engine", "voice", "seconds"}."""
    text = speakable(text)[:MAX_TTS_CHARS]
    if not text:
        raise Invalid("nothing to say")
    engine = engine or cfg["speech"]["tts"]
    if engine in ("", "none"):
        raise Invalid("no text-to-speech engine: set [speech].tts to piper or bark")
    _check(engine, "tts")
    start = time.perf_counter()
    with _LOCK:
        if engine == "piper":
            path = _model_path(cfg, "piper", voice)
            audio, used = _piper(_load("piper", path, cfg), text), path.stem
        else:
            used = voice or cfg["speech"].get("bark_voice", "")
            audio = _bark(text, used, bool(cfg["speech"].get("bark_small", True)))
    return {"audio": audio, "engine": engine, "voice": used, "seconds": round(time.perf_counter() - start, 2)}


def _piper(voice, text: str) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        if hasattr(voice, "synthesize_wav"):       # piper-tts 1.3 and later
            voice.synthesize_wav(text, w)
        else:
            voice.synthesize(text, w)
    return buf.getvalue()


def _bark(text: str, voice: str, small: bool) -> bytes:
    if small:
        os.environ.setdefault("SUNO_USE_SMALL_MODELS", "1")
    import numpy as np
    from bark import SAMPLE_RATE, generate_audio
    samples = generate_audio(text, history_prompt=voice or None)
    return wav_bytes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes(), SAMPLE_RATE)


def wav_bytes(frames: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


# ---------------------------------------------------------- reading aloud
# The TTS tab: a text (typed, from a file, or rewritten by the loaded model) cut into
# short pieces, each spoken with speak() while the one before plays.
SEGMENT_CHARS = 400
_SENTENCE = re.compile(r"(?<=[.!?…:;])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9À-Ý])")


def segments(text: str, max_chars: int = SEGMENT_CHARS) -> list[str]:
    """`text` as pieces to read one after another: sentences, joined while they are
    short, a long one cut at a comma or a space; a paragraph always starts a new piece."""
    return [s for para in paragraphs(text, max_chars) for s in para]


def paragraphs(text: str, max_chars: int = SEGMENT_CHARS) -> list[list[str]]:
    """segments(), kept in their paragraphs."""
    return [p for p in (_pieces(para, max_chars) for para in re.split(r"\n\s*\n", text)) if p]


def _pieces(para: str, max_chars: int) -> list[str]:
    out: list[str] = []
    para = " ".join(para.split())
    if para:
        piece = ""
        for sentence in _SENTENCE.split(para):
            while len(sentence) > max_chars:
                cut = max(sentence.rfind(", ", 0, max_chars), sentence.rfind("; ", 0, max_chars))
                cut = cut + 1 if cut > max_chars // 3 else sentence.rfind(" ", 0, max_chars)
                cut = cut if cut > 0 else max_chars
                if piece:
                    out.append(piece)
                    piece = ""
                out.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if piece and len(piece) + 1 + len(sentence) > max_chars:
                out.append(piece)
                piece = ""
            piece = f"{piece} {sentence}".strip()
        if piece:
            out.append(piece)
    return [s for s in out if s]


def read_file(cfg, path: Path) -> str:
    """A file's text to read aloud: text files as they are, PDFs and images through the
    knowledge base's extraction (pdftotext, OCR), within [rag]'s size limits."""
    from .context.extract import ExtractError, extract, kind_of, size_limit_mb
    if not path.is_file():
        raise NotFound(f"{path.name} is not a file")
    rag = cfg["rag"]
    kind = kind_of(path, rag) or "text"
    if path.stat().st_size > size_limit_mb(kind, rag) * 2**20:
        raise Invalid(f"{path.name} is larger than {size_limit_mb(kind, rag)} MB")
    try:
        text = extract(path, kind, rag)
    except ExtractError as e:
        raise Invalid(f"{path.name}: {e}") from None
    if not text.strip():
        raise Invalid(f"{path.name} has no text to read")
    return text


PREPARE = {"tidy": "tidied for listening", "summary": "key points"}


def prepare(h, text: str, mode: str, events=None) -> str:
    """The text rewritten by the loaded model before it is read: 'tidy' (as written, but
    nothing that sounds bad aloud) or 'summary' (its key points, to get through it faster).
    A long text goes in parts that fit the context, each streamed as it is written."""
    from . import prompts
    if mode not in PREPARE:
        raise Invalid("prepare: tidy or summary")
    text = text.strip()
    if not text:
        raise Invalid("nothing to prepare")
    system = prompts.READ_TIDY if mode == "tidy" else prompts.READ_SUMMARY
    room = max(500, (h.cfg.context_tokens - h.cfg["model"]["max_output_tokens"]) // 2)
    parts, part = [], ""
    for para in re.split(r"\n\s*\n", text):
        if part and h.counter(part + "\n\n" + para) > room:
            parts.append(part)
            part = ""
        part = f"{part}\n\n{para}".strip()
        while h.counter(part) > room:           # one paragraph longer than the room
            cut = h.counter.truncate(part, room)
            parts.append(cut)
            part = part[len(cut):].strip()
    if part:
        parts.append(part)
    done = []
    for i, p in enumerate(parts, 1):
        if events and len(parts) > 1:
            events.emit({"type": "status", "text": f"part {i} of {len(parts)}"})
        done.append(h.generate_clean(p, system, stream=events is not None, events=events).strip())
        if events:
            events.emit({"type": "text", "text": "\n\n"})
    return "\n\n".join(d for d in done if d)
