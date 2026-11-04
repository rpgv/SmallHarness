"""Speech: model pulls, Whisper/Vosk/Piper adapters (with stand-in engines, since the
packages are optional), and the web routes. A real round trip is not run here."""
import hashlib
import io
import json
import sys
import types
import wave
import zipfile
from pathlib import Path
from unittest import mock

from smallharness import speech
from smallharness.errors import HarnessError, Invalid, NotFound

from helpers import WorkspaceTest


def wav(frames: bytes, rate=16000, channels=1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class SpeechTest(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.h = self.harness(models={"dir": str(self.ws / "models")})
        self.cfg = self.h.cfg
        self.dir = speech.speech_dir(self.cfg)
        speech._LOADED.clear()
        self.addCleanup(speech._LOADED.clear)


class Models(SpeechTest):
    def test_specs(self):
        self.assertEqual(speech.parse("whisper:base"), ("whisper", "base"))
        self.assertEqual(speech.parse("Bark"), ("bark", ""))
        self.assertIsNone(speech.parse("gemma3:1b"))                   # an Ollama name, not speech
        with self.assertRaises(Invalid):
            speech.pull(self.cfg, "whisper:huge")
        with self.assertRaises(Invalid):
            speech.pull(self.cfg, "piper:not a voice")
        with self.assertRaises(Invalid):
            speech.pull(self.cfg, "vosk:../../etc")

    def test_piper_voice_files_come_from_its_folder(self):
        from smallharness.models import Pulled, Remote
        fetched = []

        def download(src, dest, mcfg, progress=None, force=False, remote=None):
            fetched.append((src.repo, src.file))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * 10)
            if progress:
                progress(10, 20)
            return Pulled(dest, 10)
        with mock.patch("smallharness.models.inspect", lambda src, mcfg: Remote("u", 10, None)), \
                mock.patch("smallharness.models.download", download):
            item = speech.pull(self.cfg, "piper:pt_PT-tugão-medium")
        self.assertEqual(fetched, [("rhasspy/piper-voices", "pt/pt_PT/tugão/medium/pt_PT-tugão-medium.onnx"),
                                   ("rhasspy/piper-voices", "pt/pt_PT/tugão/medium/pt_PT-tugão-medium.onnx.json")])
        self.assertTrue((self.dir / "piper" / "pt_PT-tugão-medium.onnx.json").is_file())
        self.assertEqual(item.path, str(self.dir / "piper" / "pt_PT-tugão-medium.onnx"))
        self.assertIn("piper:pt_PT-tugão-medium", [i.name for i in speech.local(self.cfg)])
        speech.delete(self.cfg, "piper:pt_PT-tugão-medium")
        self.assertEqual(speech.local(self.cfg), [])
        with self.assertRaises(NotFound):
            speech.delete(self.cfg, "piper:pt_PT-tugão-medium")

    def test_whisper_large_v3_has_its_own_files(self):
        from smallharness.models import Pulled, Remote
        got = []

        def download(src, dest, *a, **k):
            got.append(src.file)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"")
            return Pulled(dest, 0)
        with mock.patch("smallharness.models.inspect", lambda src, mcfg: Remote("u", 0, None)), \
                mock.patch("smallharness.models.download", download):
            speech.pull(self.cfg, "whisper:large-v3")
        self.assertIn("vocabulary.json", got)
        self.assertIn("whisper:large-v3", [i.name for i in speech.local(self.cfg)])

    def vosk_routes(self, archive: bytes, md5: str | None = None):
        listing = [{"name": "vosk-model-small-xx-0.1", "url": "https://alphacephei.com/vosk/models/vosk-model-small-xx-0.1.zip",
                    "md5": md5 or hashlib.md5(archive).hexdigest(), "size": str(len(archive))}]

        def opener(req, timeout=0):
            url = req.full_url if hasattr(req, "full_url") else req
            return Reply(json.dumps(listing).encode() if url.endswith(".json") else archive)
        return mock.patch("urllib.request.urlopen", opener)

    def test_vosk_zip_is_checked_and_unpacked(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("vosk-model-small-xx-0.1/am/final.mdl", "model")
            z.writestr("vosk-model-small-xx-0.1/conf/model.conf", "conf")
        with self.vosk_routes(buf.getvalue()):
            item = speech.pull(self.cfg, "vosk:vosk-model-small-xx-0.1")
        self.assertTrue((self.dir / "vosk" / "vosk-model-small-xx-0.1" / "am" / "final.mdl").is_file())
        self.assertEqual(item.name, "vosk:vosk-model-small-xx-0.1")
        self.assertEqual([p.name for p in (self.dir / "vosk").iterdir()], ["vosk-model-small-xx-0.1"])   # no leftovers

    def test_vosk_bad_md5_and_unknown_names(self):
        with self.vosk_routes(b"PK not really", md5="0" * 32):
            with self.assertRaises(HarnessError) as e:
                speech.pull(self.cfg, "vosk:vosk-model-small-xx-0.1")
            self.assertIn("MD5", str(e.exception))
            with self.assertRaises(NotFound):
                speech.pull(self.cfg, "vosk:vosk-model-small-zz-9")
        self.assertFalse(any((self.dir / "vosk").glob("*.part")))

    def test_status_says_what_is_missing(self):
        with mock.patch.object(speech, "installed", lambda e: False):
            st = speech.status(self.cfg)
        self.assertIn("pip install faster-whisper", st["stt_ready"])
        self.assertIn("whisper:base", [a["name"] for a in st["available"]])
        with mock.patch.object(speech, "installed", lambda e: True):
            self.assertIn("smallharness pull whisper:base", speech.status(self.cfg)["stt_ready"])


class Engines(SpeechTest):
    def test_vosk_reads_wav_and_mixes_stereo_down(self):
        rate, frames = speech.read_wav(wav(b"\x10\x00\x30\x00" * 4, 8000, channels=2))
        self.assertEqual((rate, frames), (8000, b"\x20\x00" * 4))
        with self.assertRaises(Invalid):
            speech.read_wav(b"not a wav")
        (self.dir / "vosk" / "vosk-model-small-en-us-0.15").mkdir(parents=True)
        fed = []

        class Rec:
            def __init__(self, model, rate):
                fed.append(rate)

            def AcceptWaveform(self, data):
                fed.append(len(data))
                return True

            def Result(self):
                return json.dumps({"text": "hello"})

            def FinalResult(self):
                return json.dumps({"text": "world"})
        fake = types.SimpleNamespace(KaldiRecognizer=Rec, Model=lambda p: "model", SetLogLevel=lambda n: None)
        with mock.patch.dict(sys.modules, {"vosk": fake}), mock.patch.object(speech, "installed", lambda e: True):
            r = speech.transcribe(self.cfg, wav(b"\x00\x00" * 6000), "vosk")
        self.assertEqual(r["text"], "hello hello world")
        self.assertEqual(fed, [16000, 8000, 4000])

    def test_piper_writes_a_wav_and_skips_code(self):
        (self.dir / "piper").mkdir(parents=True)
        (self.dir / "piper" / "en_US-lessac-medium.onnx").write_bytes(b"")
        said = []

        class Voice:
            def synthesize_wav(self, text, w):
                said.append(text)
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(22050)
                w.writeframes(b"\x01\x00" * 100)
        fake = types.SimpleNamespace(PiperVoice=types.SimpleNamespace(load=lambda p: Voice()))
        with mock.patch.dict(sys.modules, {"piper": fake}), mock.patch.object(speech, "installed", lambda e: True):
            r = speech.speak(self.cfg, "## Done\nSee **this**:\n```py\nx = 1\n```\nand [docs](https://x.org).")
            speech.speak(self.cfg, "again")
        self.assertEqual(said[0], "Done See this: (code omitted) and docs.")
        self.assertEqual(r["voice"], "en_US-lessac-medium")
        self.assertEqual(speech.read_wav(r["audio"])[0], 22050)
        self.assertEqual(len(speech._LOADED), 1)                            # loaded once, kept
        with self.assertRaises(Invalid):
            speech.speak(self.cfg, "**  ** #")

    def test_engine_off_or_missing(self):
        with self.assertRaises(Invalid):
            speech.transcribe(self.cfg, b"x", "none")
        with self.assertRaises(Invalid):
            speech.transcribe(self.cfg, b"x", "piper")                      # piper is TTS
        with mock.patch.object(speech, "installed", lambda e: False):
            with self.assertRaises(HarnessError) as e:
                speech.speak(self.cfg, "hi")
            self.assertIn("pip install piper-tts", str(e.exception))


class Commands(SpeechTest):
    def test_terminal_tts_and_stt(self):
        from smallharness.cli.main import main
        (self.dir / "piper").mkdir(parents=True)
        (self.dir / "piper" / "en_US-lessac-medium.onnx").write_bytes(b"")
        out = self.ws / "out.wav"
        with mock.patch.object(speech, "speak", return_value={"audio": wav(b"\x00\x00"), "engine": "piper",
                                                               "voice": "v", "seconds": 0.1}) as speak, \
                mock.patch("sys.stdout", io.StringIO()):
            code = main(["-w", str(self.ws), "-b", "mock", "tts", "hello", "there", "-o", str(out)])
        self.assertEqual((code, speak.call_args.args[1]), (0, "hello there"))
        self.assertTrue(out.is_file())
        with mock.patch.object(speech, "transcribe", return_value={"text": "hello there", "engine": "vosk",
                                                                    "model": "m", "seconds": 0.1}), \
                mock.patch("sys.stdout", io.StringIO()) as printed, mock.patch("sys.stderr", io.StringIO()):
            code = main(["-w", str(self.ws), "-b", "mock", "stt", str(out)])
        self.assertEqual((code, printed.getvalue().strip()), (0, "hello there"))
        with mock.patch("sys.stdout", io.StringIO()) as printed:
            self.assertEqual(main(["-w", str(self.ws), "-b", "mock", "stt", str(self.ws / "none.wav")]), 1)


class ReadingAloud(SpeechTest):
    def test_segments(self):
        text = "First sentence. Second one! A third?\n\nA new paragraph starts a new piece. " + "word, " * 100
        parts = speech.segments(text, max_chars=80)
        self.assertEqual(parts[0], "First sentence. Second one! A third?")
        self.assertTrue(parts[1].startswith("A new paragraph"))
        self.assertTrue(all(len(p) <= 80 for p in parts))
        self.assertEqual(" ".join(parts).split(), text.split())          # nothing lost or doubled
        self.assertEqual(speech.segments("  \n\n "), [])

    def test_read_file_and_prepare(self):
        (self.ws / "a.md").write_text("Some text.\n")
        self.assertEqual(speech.read_file(self.h_cfg(), self.ws / "a.md"), "Some text.\n")
        (self.ws / "empty.md").write_text("  \n")
        with self.assertRaises(Invalid):
            speech.read_file(self.h_cfg(), self.ws / "empty.md")
        with self.assertRaises(NotFound):
            speech.read_file(self.h_cfg(), self.ws / "none.md")
        h = self.harness(script=["Point one.", "Point two."])
        h.cfg.data["model"]["context_tokens"] = 1200
        h.cfg.data["model"]["max_output_tokens"] = 100
        long = "\n\n".join(f"Paragraph {i}. " + "words " * 150 for i in range(4))
        out = speech.prepare(h, long, "summary")
        self.assertIn("Point one.", out)
        self.assertIn("Point two.", out)                                  # in parts that fit the context
        with self.assertRaises(Invalid):
            speech.prepare(h, long, "poem")

    def h_cfg(self):
        return self.harness().cfg
