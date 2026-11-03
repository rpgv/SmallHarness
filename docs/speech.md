# Speech

Speech to text (dictation) and text to speech (reading answers aloud), run on your
machine. Code: `speech.py`, `cli/speech_cmds.py`.

| engine | does | install | models |
|---|---|---|---|
| Whisper (faster-whisper) | speech to text, many languages | `pip install faster-whisper` | `whisper:tiny`, `base`, `small`, `medium`, `large-v3`, `distil-large-v3` |
| Vosk | speech to text, small and fast | `pip install vosk` | `vosk:NAME`, any model from [alphacephei.com/vosk/models](https://alphacephei.com/vosk/models) |
| Piper | text to speech, fast | `pip install piper-tts` | `piper:VOICE`, any voice from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices), e.g. `piper:en_US-lessac-medium` |
| Bark | text to speech, expressive but slow (needs PyTorch) | `pip install suno-bark` | `bark` or `bark:small` |

Or install them through the extras: `pip install "smallharness[stt,tts]"`, and
`"smallharness[bark]"` for Bark.

## Getting models
Speech models are pulled like language models, into `<models folder>/speech/`:

```sh
smallharness pull whisper:base                  # Hugging Face (Systran), size and SHA-256 checked
smallharness pull vosk:vosk-model-small-en-us-0.15   # alphacephei.com, MD5 checked, unpacked
smallharness pull piper:en_US-lessac-medium     # the voice and its config, from Hugging Face
smallharness pull bark:small                    # Bark downloads into its own cache
smallharness speech                             # engines installed, models pulled, what is ready
smallharness delete whisper:base
```

On the web, the **Speech** card on the Models tab does the same, and also chooses the
engines, models and voice.

## Using it
- **Terminal.** `smallharness stt recording.wav` prints what was said (`--engine`,
  `--model`, `--language`). `smallharness tts "Hello" -o hello.wav [--play]` writes a
  WAV, or reads the text from standard input (`--engine`, `--voice`).
- **Web, chat.** The 🎤 in the corner of the message box records while it is red;
  press it again to stop, and the text goes into the box for you to check and send.
  The page records 16 kHz WAV itself, so nothing else is needed. Browsers allow the
  microphone only on `localhost` or over HTTPS, so from another device on the network
  dictation needs HTTPS. *speak* on an answer reads it aloud (code blocks and links
  are left out); press it again to stop.

## Settings (`[speech]`)
| key | default | |
|---|---|---|
| `stt` | `whisper` | `whisper`, `vosk` or `none` (hides the 🎤) |
| `whisper_model` | `base` | a pulled size, or a folder |
| `vosk_model` | `vosk-model-small-en-us-0.15` | a pulled model, or a folder |
| `language` | `""` | Whisper: empty detects the language; or `en`, `pt`, … |
| `tts` | `piper` | `piper`, `bark` or `none` |
| `piper_voice` | `en_US-lessac-medium` | a pulled voice, or an `.onnx` file |
| `bark_voice` | `v2/en_speaker_6` | a Bark speaker preset |
| `bark_small` | `true` | Bark's smaller models |
| `device` | `cpu` | Whisper: `cpu`, `cuda` or `auto` |

A model stays loaded after its first use, so later calls are faster. Whisper reads WAV
directly; other formats go through PyAV, which comes with faster-whisper.
