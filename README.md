# SmallHarness

A rigid harness for small local models, built for Google's
[LiteRT-LM](https://github.com/google-ai-edge/LiteRT-LM), with
[Ollama](https://ollama.com) as a second engine. Small models do best with a narrow
job and a clean context, so every mode keeps the model's context small. There are three
modes:

- **Chat**: talk with the model. It gets no tools unless a message asks for one with a
  command (`/read src/app.py`). Pin answers to keep them in mind, reply in threads with
  a smaller context, and rewind to any earlier point.

- **Research**: ask questions of your files and notes. Answers cite the excerpts they
  rest on, like `[3]`.
- **Planner**: turn one sentence into a step-by-step plan, refine it, and run it with
  tools.

Each model call holds only what the mode puts in: a research question its excerpts, a
chat reply its conversation (or just its thread). The harness keeps the context in budget, finds the right excerpts, checks tool calls (risky
ones ask you first), and runs as many model instances as your RAM allows.

```
┌───────────────────────────────────────────────────────────┐
│ SmallHarness · research  v1.0.0                           │
│   ask questions of your files and notes                   │
│   model    gemma-4-E2B-it · litert(server) · 4096 ctx     │
└───────────────────────────────────────────────────────────┘
research › when does the release train leave?
◆ The release train leaves every Friday at noon [1].
  A1 · refs [1] · /attach 1 keeps it in mind · /bibliography
```

## Install

Python 3.11 or newer. The harness itself uses only the standard library; the extras add
the model runtime and optional features.

```sh
git clone https://github.com/rpgv/SmallHarness && cd SmallHarness
pip install -e ".[litert]"     # LiteRT-LM, the recommended engine
# extras: embed (search by meaning), ram (better RAM checks), all (everything)
```

## Try it without a model

The `mock` backend answers instantly, so you can look around first:

```sh
smallharness --backend mock         # choose a mode in the terminal
smallharness --backend mock web     # or open the web interface
```

## Run a real model

```sh
smallharness models                 # models you have, and ones you can download
smallharness pull gemma-4-E2B-it    # download one (resumable, checksum-verified)
smallharness check -m gemma-4-E2B-it   # will it fit in RAM?

cp smallharness.example.toml smallharness.toml   # starts `litert-lm serve` for you
smallharness -m gemma-4-E2B-it      # choose a mode and go
```

Using Ollama instead? With `ollama serve` running:

```sh
smallharness -b ollama pull qwen3:0.6b
smallharness -b ollama -m qwen3:0.6b
```

## First steps

**Chat.** Talk with the model, and bring in a file when you need it:

```sh
smallharness chat
chat › /read README.md\
. what does this project do?
```

**Research.** Add some files, then ask:

```sh
smallharness index docs/ manual.pdf
smallharness ask "when does the release train leave?"
```

**Planner.** Plan a change, add detail, and run it:

```sh
smallharness plan "add a --verbose flag" --expand 2 --run fresh
```

**Web interface.** `smallharness web` opens all of this in your browser (chat with
threads beside the conversation), plus a Notes tab, model downloads, settings, and **Relics**: the files your plans
wrote, ready to edit.

Your settings go in `smallharness.toml`, and what the harness keeps for a project goes
in `.smallharness/`. In any terminal mode, `/help` lists the commands.

## Documentation

The [docs](docs/README.md) cover each part in depth:
[Chat](docs/chat.md) · [Research](docs/research.md) · [Planner](docs/planner.md) ·
[Web interface](docs/web.md) ·
[Commands](docs/commands.md) · [Models](docs/models.md) ·
[Engines](docs/engines.md) · [Configuration](docs/configuration.md) ·
[Tools](docs/tools.md) · [Development](docs/development.md)

## Tests

```sh
python -m unittest discover -s tests
```
