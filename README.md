# SmallHarness

A rigid harness for small local models, targeting Google's
[LiteRT-LM](https://github.com/google-ai-edge/LiteRT-LM), with
[Ollama](https://ollama.com) as a second engine. Small models do best with a
narrow job and a clean context, so SmallHarness has no open-ended chat. It has
three modes, each a direct way to use a model:

- **Research**: a knowledge base of your files and notes. Ask a question and the
  harness finds the excerpts, the model answers from them, and the answer cites
  them by number, like `[3]`. The numbers lead to a bibliography.
- **Planner**: a plan for a code change from one sentence. Expand its steps pass by
  pass into more detail, edit them as text, and run the plan.
- **Recipe**: a recipe, written section by section, turned into a file: code, or
  Markdown text. Each section is written on its own.

Every model call starts from a clean context: a one- or two-line system prompt and
only what the mode puts in. Research gets the excerpts it found, and the answers you
chose to attach. Plan runs keep a fresh or a continuous context, as you choose. The
harness keeps the context in budget, retrieves knowledge, parses and gates tool
calls, and sizes model instances to the RAM available.

Python 3.11+, standard library only. LiteRT-LM, psutil, fastembed and numpy are optional.

```
┌───────────────────────────────────────────────────────────┐
│ SmallHarness · research  v3.0.0                           │
│   ask questions of your files and notes                   │
│   model    gemma-4-E2B-it · litert(server) · 4096 ctx     │
└───────────────────────────────────────────────────────────┘
research › when does the release train leave?
◆ The release train leaves every Friday at noon [1].
  A1 · refs [1] · /attach 1 keeps it in mind · /bibliography
```

## Quick start

```sh
pip install -e .                    # or run in place: python -m smallharness

smallharness --backend mock         # try it without a model: choose a mode
smallharness research               # or go straight to a mode
smallharness plan                   # (also: smallharness recipe)

smallharness models                 # downloaded models, and ones available to pull
smallharness pull gemma-4-E2B-it    # download one into models/ (resumable, SHA-256 checked)
smallharness check -m gemma-4-E2B-it          # RAM report: how many instances fit

# Recommended: let the harness run `litert-lm serve` (set in smallharness.toml)
#   [litert.server]
#   launch = true
smallharness -m gemma-4-E2B-it      # a short name, or a path to any model file

# One-off use from the shell
smallharness index docs/ manual.pdf                 # add to the knowledge base
smallharness ask "when does the release train leave?"
smallharness plan "add a --verbose flag" --expand 2 --run fresh
smallharness recipe intro --text                    # write a recipe as Markdown
smallharness notes "Remember the Friday release #ops"   # a note, no model loaded
smallharness web                    # the visual interface, in your browser

# Or with Ollama (running: `ollama serve`)
smallharness -b ollama pull qwen3:0.6b
smallharness -b ollama -m qwen3:0.6b
```

Copy `smallharness.example.toml` to `smallharness.toml` in your project to set
defaults. Harness state lives in `<project>/.smallharness/`.

### Models (`models.py`, `engines.py`)
`engines.py` is the one place that knows LiteRT-LM from Ollama: model names, the
server an engine is reached at, the context window, and listing, downloading and
deleting models. The terminal and the web interface share it.

Model files live in one folder: `models/` in the SmallHarness checkout (git-ignored),
or, for an installed package with no checkout, `~/.local/share/smallharness/models/`.
Set `[models].dir` to use another folder.

- **`smallharness models`** (or `/models`) lists the downloaded models with their size,
  maximum context (read from the file) and where they came from, marks the one in use,
  and lists the built-in names not downloaded yet.
- **`smallharness pull NAME`** (or `/pull NAME`) downloads from Hugging Face. `NAME` is a
  built-in name (`gemma-4-E2B-it`, `gemma-4-E4B-it`, `gemma3-1b-it`, `qwen3-0.6b`,
  `qwen2.5-1.5b-instruct`, `granite-4.0-350m`), `hf:OWNER/REPO/FILE`, or a
  huggingface.co file URL.
  - Before downloading it says whether the model fits in this machine's RAM, and asks
    first if it cannot run here at all (`--yes` skips the question).
  - The file is written as `NAME.part` and renamed only after its size and SHA-256
    match what Hugging Face publishes. An interrupted download (Ctrl-C, dropped
    connection) resumes when you run the same `pull` again.
  - Gated models (e.g. `gemma3-1b-it`) need their license accepted on Hugging Face and
    a token in `HF_TOKEN` (or `[models].hf_token`). The token is sent to Hugging Face
    only, not to the CDN the download is redirected to.
- **`smallharness delete NAME`** (or `/delete NAME`) removes a model from the folder,
  with an unfinished `.part` download of it, after asking (`--yes` skips the question).
  It only deletes files in the model folder, and `/delete` refuses the model in use.
  If the model was imported for server mode, the LiteRT-LM store still links the same
  file; it says so, and `litert-lm delete ID` frees that space.
- **Short names.** `-m`, `/model` and `[model].path` take a built-in name, a file
  name, or a file name without its suffix, and find the file in the model folder.
  Paths to files anywhere else still work.
- **Server mode.** `litert-lm serve` only serves models in its own store. With
  `[models].auto_import` (the default), a model file loaded from anywhere is imported
  there (`litert-lm import FILE ID`) before the server starts, under its file name
  without suffix, and the server is asked for that ID. The import is a hard link,
  so it takes no extra disk space on the same drive. If the store already holds a
  *different* model under that ID, the harness stops and says so rather than
  replacing it. Setting `[litert.server].model` (or `/model … id=X`) serves that
  model instead, as before.

### Workspace trust
A project can bring code that SmallHarness would run: tool files in
`.smallharness/tools/`, and commands or tool folders in its `smallharness.toml`
(`[litert.server].command`, `[litert.cli].command`, the `[rag.ocr]` commands,
`[tools].extra_dirs`). It can also name servers that would receive your data: where
downloads (and your Hugging Face token) go (`[models].hf_endpoint`), and where prompts
and indexed files are sent (`[litert.server].base_url`, `[ollama].base_url`,
`[rag.embed].base_url`). These are used only once you trust the workspace, so opening
someone else's repository does not run its code or send your data elsewhere.

- In an interactive session the harness lists that code and asks. Your answer is
  stored in `~/.config/smallharness/trusted.json` with a fingerprint of the code,
  so any change (a new tool file, an edited command, e.g. after a `git pull`) asks
  again.
- Without trust, the tool files are not imported and those settings fall back to
  your own config or the defaults. Everything else in the file still applies.
- `--trust` trusts the workspace for one run without saving, e.g. for `ask` in scripts.
- Your own config (`~/.config/smallharness/config.toml`, or a file passed with
  `--config`) is always trusted.

## How it works

### Resources and instances (`resources.py`, `pool.py`)
The harness reads total and available RAM (from psutil, `/proc/meminfo` with
cgroup limits, `vm_stat`, or Win32) and estimates the RAM one instance needs:
the model file size × `overhead_factor`, plus the KV cache for the context
size (exact from the model file when it can be read, see below; estimated
otherwise). It refuses to start a model that does not fit. `ModelPool` starts
more instances only while they fit (`/spawn N`, `/resources`), and plan tasks
can run on several instances in parallel.

To turn this off, set `[resources].manage_ram = false`. The harness then runs
exactly one instance and never refuses to load. `/resources` still shows the
estimate, with a warning if the model looks too big.

### Context size (`modelinfo.py`)
With `[model].context_tokens = "auto"` (the default), the harness reads the
context size from the `.litertlm` file. It uses the metadata's `max_num_tokens`
field when the model sets it, and otherwise the shape of the KV-cache tensors in
the model, which fixes how many tokens it can hold. For gemma-4-E2B-it that is
32,003 tokens. Only the file header and tensor tables are read (about 0.2 s).

- **auto** takes the model's maximum, capped at `auto_context_max` (8192 by
  default, because a long prompt can take minutes to process on a CPU). When RAM
  is managed, it is lowered until one instance fits. Set `auto_context_max = 0`
  for no cap.
- **A number** is used as given. If it is above the model's maximum, the
  maximum is used and a warning is shown.
- The context size is passed to the runtime: `--max-num-tokens` in command-line
  mode, and the engine in Python mode where the installed version accepts it.
  `litert-lm serve` sizes its own window, so in server mode the harness only
  keeps its prompts within the size.
- **RAM:** the KV-cache tensors also give the cache's exact size per token
  (9 KB for gemma-4-E2B-it, int8), which the RAM check uses in place of the
  rough estimate from the model size.
- **Token counts** are estimated on the safe side (about 3.5 characters per
  token). When the server reports the real prompt size (the `usage` field of
  OpenAI-style replies), the estimate is corrected for the model as the session
  goes on. `/status` shows which method is in use.
- `check`, `/resources` and `/model` show the context size, how it was chosen,
  and what the file says. Without a readable `.litertlm` file (an imported
  model ID, or a `.task` file), the harness uses 4096 and says so.

### Research (`research.py`)
Research works on a **knowledge base**: the files and notes you add, kept in one
index (`.smallharness/rag/project.sqlite`).

- **Adding.** `/add PATH` (or `smallharness index PATH…`, or the Library in the web
  interface, which also takes uploads) indexes a file or a whole folder. It skips
  `.git`, `node_modules`, virtualenvs and files that have not changed. Accepted are
  the types in `[rag]`: by default `.txt`, `.md`, `.py`, `.sh`, PDFs and images.
  - **Images** are read with OCR (`tesseract`).
  - **PDFs** are read page by page. With `pdf_mode = "auto"`, a page keeps its own
    text when it has some, and scanned pages are OCR'd (`pdftoppm`, then
    `tesseract`); `pdf_mode = "always"` OCRs every page. Install poppler and
    tesseract (`brew install tesseract poppler`, or `apt install tesseract-ocr
    poppler-utils`). Without them, PDFs and images are skipped with a message.
  - **Notes** (`/note TEXT`, `/notes`, or the Library) are part of the knowledge base.
- **Asking.** Type a question. The harness searches for the best excerpts (k =
  `[research].k`) and the model answers from those alone, in a clean context: the
  system prompt, the numbered excerpts, any attached answers, and the question. No
  history and no tools. If nothing matches, the model is not asked.
- **References.** The answer cites excerpts as numbers only, like `[3]`. A number
  means the same excerpt for the whole research session, and leads to the session's
  bibliography, a temporary file (`.smallharness/research/<id>.md`) with each
  excerpt's source, line and text. In the terminal, `/bibliography [N]`; on the web,
  the Bibliography pane, or click a `[n]`.
- **Attaching (CAG).** The context stays clear by default. `/attach N` (or Attach on
  the web) keeps answer N in the context of later questions, until `/detach N`.
  Attached answers share `[research].attached_budget` of the window.
- **Scope.** `/scope FILE…` (or the checkboxes in the Library) limits questions to some
  files, folders or notes; `/scope all` searches everything again. `/find WORDS`
  shows the excerpts a question would get, without asking the model.

**How the knowledge base is searched** (`context/rag.py`). Adding may take a while
so that asking is fast:
- **Chunks follow the text's structure.** In code, a new function or class (with its
  decorators) starts a chunk; in Markdown, a heading does. Small sections stay
  together, and other text is split at blank lines.
- **Keywords and meaning.** Every chunk is scored by keywords (BM25, with light
  word-form matching: "deployed" finds "deploy") and, with an embedding model, by
  meaning. The two rankings are merged (Reciprocal Rank Fusion), so exact names
  still match while "how do I undo a release" finds "run the rollback script".
- **Fast searching.** Per-term posting lists and the vectors, packed into one
  matrix, are built once per change. A question touches only the chunks that
  contain its words, plus one matrix multiplication.
- **Embeddings** (`[rag.embed].backend`, default `auto`): fastembed in process if it
  is installed (`pip install fastembed`, about 70 MB for `bge-small-en-v1.5`, with
  its RAM reserved in the RAM check); otherwise the model's own server
  (`/v1/embeddings`; for Ollama, `ollama pull nomic-embed-text`); otherwise keywords
  only, with a warning. `server` and `fastembed` choose one explicitly, `none` turns
  them off. Changing the embedding model re-embeds on the next `/add`.
  Matches by meaning count only above `min_similarity` (automatic: 0.5 for bge
  models, which score even unrelated text 0.35–0.5, else 0.3) and within
  `similarity_margin` of the best match, so an unrelated question still finds
  nothing. `smallharness check --probe` tests the embedder and suggests a value.
- **Storage.** The index is an SQLite file (standard library). A change writes only
  what changed, and several processes can share it: the terminal,
  `smallharness notes` and the web interface each see the others' changes. Indexes
  from earlier versions (`.json`) are imported once.

### Planner (`plans.py`, `planning.py`)
Type a goal and the model writes a plan in a clean context, one step per line:
`N. [tool] what to do (after: N)`.

- **Expanding.** `/expand [N,M] [times=K]` (or Expand on the web) runs one pass, or K,
  over all steps or the ones named. Each pass rewrites a step's details in more
  depth: 3 lines on the first pass, then 6, 12, up to 24. Each step gets its own
  clean-context call that sees the goal, the step and its current details. Steps
  are expanded in parallel on as many model instances as RAM allows.
- **Editing.** A plan is plain text: each step on an unindented line, its details on
  the indented lines below it. Edit it with `/edit` (in `$EDITOR`, or typed) or in
  the web editor (Tab indents), or write one yourself with `/draft`. A step left as
  it was keeps its status and its expansion depth.
- **Running.** `/run fresh` gives each step a new, empty context: the plan, the
  step's details and the results of the steps it depends on. `/run continuous`
  carries one context across all steps, refreshed as it fills. `parallel` runs
  independent steps at the same time, as RAM allows (fresh runs). Each step ends with
  `DONE: …` or `FAILED: …`; when a step fails, the steps that depend on it are
  skipped. Runs use the workspace tools below and nothing else: no knowledge base.

### Recipes (`recipe.py`)
A recipe is turned into a file one section at a time. Write the sections as
paragraphs separated by an empty line, each describing what to write. Optional
first lines `target: FILE` and `mode: code|text` set the file and the kind:

```
target: src/shapes.py

area(radius: float) -> float
returns the area of a circle, using math.pi

class Circle
holds a radius; area() -> float uses area()
```

- **Code or text.** `code` writes functions, classes and statements: Python imports
  are gathered at the top, and each Python section is syntax-checked. `text` writes
  Markdown paragraphs, for documents. The mode follows the target (`.md`, `.txt`:
  text), or set it with `mode:`, `text`/`code` on `/write`, `--text`/`--code`, or the
  switch on the web. Each mode has its own short system prompt.
- **Restricted on purpose.** Each section is written by its own clean-context call
  that reads only that section. Its only tool is `risky_write_file`, which you allow
  or deny once, before the first section. A section that fails is retried once with
  the reason, then kept in the file under a comment saying why.
- **One file, rewritten whole.** Every section has its place in the target, in
  recipe order. A run writes only sections that are new, changed or failed (`all`
  writes them all, `only=N,M` just those); edit the recipe, not the output.
- **Parallel.** `parallel` writes several sections at once on as many model instances
  as RAM allows.
- **The code library** (code mode, `snippets.py`). Every section written in code is kept
  as its own file in `.smallharness/snippets/<language>/`, one library per language
  (from the target's extension), indexed with a brief description: the section's first
  line, with the whole section text and the code. The target file is built by joining
  these files in recipe order. Before a section is written, its language's library is
  searched:
  - a snippet written for a section that says nearly the same thing (by default 85% of
    the same words, `[recipe].reuse_similarity`) is reused as is, with no model call;
  - otherwise the most similar snippets (`[recipe].library_k`, default 3) are shown to
    the model, which can reply `REUSE <id>` to use one unchanged, or write new code.

  New code joins the library; identical code is kept once and remembers every recipe
  that used it. A recipe never reuses its own other sections (each section sees only
  itself, and the file would hold the code twice). Take a snippet out and the sections
  that used it are written again. Browse with `/library [LANGUAGE [WORDS]]`,
  `/library show|remove LANGUAGE ID`, or the Code library panel in the Recipes tab.
  `[recipe].library = false` turns it off.
- `/new NAME [to=FILE] [text|code]`, `/show NAME`, `/edit NAME`, `/write NAME|PATH
  [to=FILE] [text|code] [all] [only=N,M] [parallel]`, `smallharness recipe NAME
  [--to FILE] [--text|--code] [--all] [--only N,M] [--parallel] [-y]`, and the
  **Recipes** tab of the web interface.

### Tools (`tools/`), used by plan runs
Plan runs are the only place the model calls tools. A tool is one `.py` file; add
your own in `.smallharness/tools/` (or `[tools].extra_dirs`). See
`examples/tools/safe_word_count.py`:

```python
NAME = "safe_word_count"          # safe_* = type 2, runs freely
DESCRIPTION = "Count lines, words and characters in a workspace file."
PARAMS = {"path": {"type": "str", "desc": "file path"}}   # add "default" to make optional
def run(args, ctx): ...
```

**The risk is part of the name.** `risky_*` tools are type 1 and ask for permission
before every call, with no "always allow" option. `safe_*` tools are type 2 and run
without asking. A file with any other name is rejected. The `safe_` file tools only
reach paths inside the workspace. Tools in the workspace's own
`.smallharness/tools/` load only if the workspace is trusted (see
[Workspace trust](#workspace-trust)). Calls are parsed leniently, because small models
often send malformed JSON; a bad call gets back the error and the tool's usage.

| tool | type |
|---|---|
| `safe_read_file`, `safe_list_files`, `safe_search_files` | 2 |
| `risky_run_command`, `risky_write_file` | 1 |

### Notes (`notes.py`, `notemode.py`)
Notes are Markdown files in `.smallharness/notes/<n>.md`, indexed into the knowledge
base as soon as they are saved. Tags are optional: write `#word` anywhere (`# Heading`
is not a tag). Notes edited or deleted outside the harness are picked up the next
time they are opened. `smallharness notes` is note mode without loading a model:
type a line to save it; `/new`, `/list [#tag]`, `/search WORDS [#tag]`, `/show N`,
`/edit N`, `/append N TEXT`, `/delete N` and `/tags` do the rest. In research,
`/note TEXT` saves one and `/notes` opens note mode. CAG files from earlier versions
(`.smallharness/cag/`) are imported once as notes.

### Visual interface (`web/`)
`smallharness web` starts a local web server and opens it in your browser. Its tabs
are the modes:
- **Research**: *Ask* (answers with `[n]` links, Attach on each answer, the attached
  ones above the question box, a speed line), *Library* (add or upload files, notes,
  checkboxes for the scope) and *Bibliography* (each reference, and its excerpt).
- **Planner**: create, draft or edit plans with their details; expand (which steps,
  how many passes, parallel); run fresh or continuous.
- **Recipes**: write, save and run recipes, code or text, optionally in parallel.
- **Models** (use, pull with progress, delete) and **Settings** (status, every
  setting, the planner's tools).

The model loads when first needed; `--load` loads it before the page opens. **Stop**
ends whatever the model is doing; risky tools ask in a dialog.

- **No key to type.** It listens on `127.0.0.1` only. Requests that name another host
  are refused (DNS rebinding); requests from another site's page are refused (the
  Origin check); and every request with a body must be JSON (an upload, binary),
  which other sites cannot send without the browser asking first. `--host` makes it
  reachable from other machines: then anyone on that network can use it.
- Model work runs one request at a time. Standard library only: no build step,
  nothing loaded from the internet (the Roboto Mono font, Apache 2.0, ships with it).

### Several instances
The planner (expanding, `parallel` runs) and recipes (`parallel`) start more model
instances when there is more than one thing to do: as many as RAM allows with
`[resources].manage_ram` on, otherwise one. `/resources` (or Settings on the web)
shows how many fit.

### Changing the model and settings while running
- **`/model NAME`** switches to another model: a name from `/models`, or a path to a
  model file. `/model` alone shows the current one. Options: `ctx=N` or `ctx=auto`,
  `out=N` (output tokens), `template=plain|gemma`, `name=X`, `mb=N` (model size for
  the RAM estimate). In server mode the first word is the model ID the server serves
  (`/model gemma-4-E4B-it`), and `path=FILE` sets the file used for the RAM estimate.
- **`/config set KEY=VALUE`** changes a setting for this run only, e.g.
  `/config set research.k=8`. Values use TOML syntax (`true`, `4096`, `["a", "b"]`).
  **`/config reload`** re-reads `smallharness.toml`; command-line flags such as `-m`
  still take precedence. `/config show [SECTION]` lists the settings.
- Changing the model file, `context_tokens` or anything under `[litert]` restarts the
  model, after the RAM check. If the new model fails to start, the previous settings
  and model are restored.

## Commands

Start with `smallharness` and choose a mode (or `smallharness research|plan|recipe`).
In every mode: `/help` `/mode` (back to the menu) `/status` `/model` `/models`
`/pull` `/delete` `/config` `/resources` `/exit`. Type `\` at the end of a line to
continue on the next one.

| mode | plain text | commands |
|---|---|---|
| research | a question | `/add` `/files` `/remove` `/note` `/notes` `/scope` `/find` `/bibliography` `/attach` `/detach` `/answers` |
| planner | a goal to plan | `/show` `/plans` `/load` `/draft` `/edit` `/expand` `/run` `/tools` |
| recipe | (hint) | `/list` `/new` `/show` `/edit` `/write` `/library` |

The interface is shades of blue; red marks risky (type 1) tools and nothing else.

## LiteRT-LM notes
`[litert].mode` chooses how the harness talks to LiteRT-LM. On `auto` it uses
the server if `[litert.server].launch` or `base_url` is set, otherwise the
Python package. It never falls back to command-line mode on its own.

- **Server mode (recommended).** `litert-lm serve` runs an OpenAI-compatible API
  server (`/v1/models`, `/v1/chat/completions`, `/v1/embeddings`). The model
  stays loaded, output streams, the prompt is sent in the request body, and
  `max_tokens` is sent with each request. The server takes no model argument;
  each request names an imported model ID (`litert-lm import`,
  `litert-lm list`). The harness sends the whole assembled window as one user
  message, because it owns the context.
  - **`launch = true`**: the harness starts one server per model instance and
    stops it on exit. The default command binds to `127.0.0.1`, because
    `litert-lm serve` listens on `0.0.0.0` (every network interface) by
    default. The first server uses port `9379`; later instances get free
    ports. Each server holds a copy of the model, so the RAM check applies as
    for in-process instances. Set `[model].path` to the model file, or set
    `[resources].model_mb`; with neither, only one instance runs.
  - **`base_url`**: connect to a server you started yourself. Its RAM is not
    checked; `max_concurrent` sets how many requests it receives at once.
- **Python mode.** Uses `litert_lm.Engine(model).create_conversation().send_message(prompt)`
  and opens a fresh conversation per call. The adapter in `backends/litert.py`
  is defensive about API differences between versions. If your installed
  version differs, adjust `_open_engine` and `_extract_text`.
- **Command-line mode (opt-in, last resort).** Set `mode = "cli"`. The default
  command is `litert-lm run {model} --prompt {prompt} --max-num-tokens
  {context_tokens} --cache disk`. Every call starts a process that reloads the
  model, so it is slow and does not stream, and the banner warns about this.
  `--cache disk` keeps compiled artifacts between runs to cut that cost.
  - **Prompt delivery:** `litert-lm run` accepts the prompt only through
    `--prompt`, so it is a command-line argument. It is visible in `ps` and
    limited in length; over-long prompts are refused. `prompt_via` can also be
    `stdin` or `file` (`{prompt_file}`, a private temp file deleted after the
    call) for wrapper scripts that support them.
  - **Context and output length:** `--max-num-tokens` sets the context size
    (the KV cache). `run` has no flag that limits output length.
  - **Output:** terminal colour codes are stripped, and `output_regex` keeps
    only the reply.
  - **RAM:** runs are limited by free RAM measured while nothing is running. If
    the model size is unknown, only one runs at a time.
  - **Testing your setup:** `smallharness check --probe` shows the command,
    the raw output, the extracted answer and the load time.
- **Tool calling.** LiteRT-LM has native tool calling, but SmallHarness uses its
  own text protocol so that parsing, validation and permission checks stay in
  the harness and work the same way with any model.
- **Token counts** are a conservative estimate unless the backend exposes a
  tokenizer (`Backend.count_tokens`).

## Ollama notes
Set `[model].backend = "ollama"` (or `-b ollama`) to use a running Ollama server
(`[ollama].base_url`, default `http://127.0.0.1:11434`). The harness does not start
Ollama itself.

- **Models** are Ollama's own (GGUF, by name such as `qwen3:0.6b`), not the
  `.litertlm` files in `models/`. With the Ollama backend, `models`, `pull` and
  `delete` (and `/models`, `/pull`, `/delete`) act on Ollama's store. `-m NAME`,
  `/model NAME` and `[ollama].model` choose the model; empty means the first one
  Ollama lists.
- **Native API.** The harness uses `/api/chat`, not Ollama's OpenAI-compatible
  API, because only the native API takes the context size per request. It sends
  the window as `num_ctx`, the output limit as `num_predict`, and the stop words,
  with every request. Without `num_ctx` Ollama uses its small default and quietly
  cuts longer prompts.
- **Context size.** `context_tokens = "auto"` takes the maximum Ollama reports
  for the model (`/api/show`), capped at `auto_context_max`. When Ollama runs on
  this machine, the model's size is used to lower the window until it fits in
  free RAM; for a remote Ollama, local RAM is not considered.
- **RAM and instances.** Ollama owns the model's RAM, like a LiteRT-LM server
  you started yourself. `[ollama].max_concurrent` sets how many requests it gets
  at once; match it to `OLLAMA_NUM_PARALLEL`.
- **Token counts** stay an estimate. Ollama has no tokenizer endpoint, and its
  `prompt_eval_count` leaves out the cached part of the prompt, so it is not used
  to calibrate.
- **Reasoning models** (e.g. Qwen3): a leading `<think>…</think>` block is left
  out of the reply. `[ollama].think = false` asks Ollama not to reason at all,
  which is faster on small models.
- **Chat template.** Ollama applies the model's own template, so keep
  `[model].template = "plain"`.
- **Embeddings.** Without fastembed installed, research uses Ollama's own
  `/v1/embeddings` with `nomic-embed-text` (`ollama pull nomic-embed-text`); set
  `[rag.embed].model` to use another embedding model.

## Tests

```sh
python -m unittest discover -s tests
```
