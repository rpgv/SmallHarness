"""Speech from the terminal: `smallharness stt FILE`, `smallharness tts TEXT`,
`smallharness speech`, and speech models in `pull`, `delete` and `models`."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .. import speech, ui
from ..config import Config
from ..errors import HarnessError
from .model_cmds import _Progress


def run_speech(cfg: Config, args) -> int:
    try:
        if args.cmd == "speech":
            print_speech_models(cfg)
            return 0
        if args.cmd == "stt":
            audio = Path(args.audio).expanduser()
            if not audio.is_file():
                print(ui.err(f"no file {audio}"))
                return 1
            r = speech.transcribe(cfg, audio.read_bytes(), args.engine, args.model, args.language)
            print(r["text"])
            print(ui.dim(f"  {r['engine']} {r['model']} · {r['seconds']} s"), file=sys.stderr)
            return 0
        text = " ".join(args.text) if args.text else sys.stdin.read()
        r = speech.speak(cfg, text, args.engine, args.voice)
        out = Path(args.output).expanduser()
        out.write_bytes(r["audio"])
        print(ui.ok(f"✓ {out}") + ui.dim(f"  {r['engine']} {r['voice']} · {r['seconds']} s"))
        if args.play:
            play(out)
        return 0
    except HarnessError as e:
        print(ui.err(str(e)))
        return 1


def play(path: Path) -> bool:
    for cmd in (["afplay"], ["paplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd + [str(path)], check=False)
            return True
    print(ui.warn("no audio player found (afplay, paplay, aplay or ffplay); open the file yourself"))
    return False


def print_speech_models(cfg: Config, brief: bool = False) -> None:
    st = speech.status(cfg)
    s = st["settings"]
    print(ui.dim(f"speech models in {st['dir']}"))
    for e, info in st["engines"].items():
        mark = ui.ok("installed") if info["installed"] else ui.dim(f"not installed: pip install {info['pip']}")
        print(f"   {e.ljust(8)} {info['kind'].upper()}  {mark}")
    for i in st["local"]:
        print(f" {ui.accent('●')} {ui.sky(i['name'])}  {i['size_mb']} MB")
    if not st["local"]:
        print("   (no speech models yet)")
    print(ui.dim(f"   STT: {s['stt']} ({s['whisper_model'] if s['stt'] == 'whisper' else s['vosk_model']})"
                 + (f" · {st['stt_ready']}" if st["stt_ready"] else " · ready")))
    print(ui.dim(f"   TTS: {s['tts']} ({s['piper_voice'] if s['tts'] == 'piper' else s['bark_voice']})"
                 + (f" · {st['tts_ready']}" if st["tts_ready"] else " · ready")))
    if not brief:
        print(ui.dim("available to pull (smallharness pull NAME):"))
        w = max(len(a["name"]) for a in st["available"])
        for a in st["available"]:
            print(f"   {a['name'].ljust(w)}  {a['size_mb']:>5} MB  {ui.dim(a['note'])}")
    else:
        print(ui.dim("   more: smallharness speech"))


def pull_speech(cfg: Config, spec: str, force: bool = False) -> bool:
    bar = _Progress()
    try:
        item = speech.pull(cfg, spec, bar, force)
    except HarnessError as e:
        bar.end()
        print(ui.err(str(e)))
        return False
    except KeyboardInterrupt:
        bar.end()
        print(ui.pale("interrupted; run the same pull again"))
        return False
    bar.end()
    print(ui.ok(f"✓ {item.name}") + ui.dim(f"  {item.size_mb} MB in {item.path}"))
    engine, name = speech.parse(item.name)
    key = {"whisper": "whisper_model", "vosk": "vosk_model", "piper": "piper_voice"}.get(engine)
    if key and cfg["speech"].get(key) != name:
        print(ui.dim(f"  use it: set [speech].{key} = \"{name}\"" + (f" and [speech].{speech.ENGINES[engine][0]} = \"{engine}\"")))
    if not speech.installed(engine):
        print(ui.warn(f"{engine} is not installed: pip install {speech.ENGINES[engine][2]}"))
    return True


def delete_speech(cfg: Config, spec: str, assume_yes: bool = False) -> bool:
    if not ui.confirm(f"Delete {spec}?", assume_yes, "not deleted (use --yes to skip the question)"):
        return False
    try:
        item = speech.delete(cfg, spec)
    except HarnessError as e:
        print(ui.err(str(e)))
        return False
    print(ui.ok(f"deleted {item.name}"))
    return True
