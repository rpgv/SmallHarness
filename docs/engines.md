# Engines

SmallHarness targets [LiteRT-LM](https://github.com/google-ai-edge/LiteRT-LM), with
[Ollama](https://ollama.com) as a second engine (`[model].backend`, or `-b`). The
`mock` backend answers without a model, for trying things out and for tests.

Both engines share two rules:
- **Tool calling.** Both have native tool calling, but SmallHarness uses its own text
  protocol so that parsing, validation and permission checks stay in the harness and
  work the same way with any model.
- **Token counts** are a conservative estimate unless the backend exposes a tokenizer
  (`Backend.count_tokens`).

## LiteRT-LM
Install it with `pip install "smallharness[litert]"`: that gives the `litert-lm`
command and its Python API. `[litert].mode` chooses how the harness talks to it. On
`auto` it uses the server if `[litert.server].launch` or `base_url` is set, otherwise
the Python package. It never falls back to command-line mode on its own.

### Server mode (recommended)
`litert-lm serve` runs an OpenAI-compatible API server (`/v1/models`,
`/v1/chat/completions`, `/v1/embeddings`). The model stays loaded, output streams, the
prompt is sent in the request body, and `max_tokens` is sent with each request. The
harness sends the whole assembled window as one user message, because it owns the
context.

- **`launch = true`**: the harness starts one server per model instance and stops it on
  exit. The default command binds to `127.0.0.1`, because `litert-lm serve` listens on
  `0.0.0.0` (every network interface) by default. The first server uses port `9379`;
  later instances get free ports. Each server holds a copy of the model, so the RAM
  check applies as for in-process instances. Set `[model].path` to the model file, or
  set `[resources].model_mb`; with neither, only one instance runs.
- **`base_url`**: connect to a server you started yourself. Its RAM is not checked;
  `max_concurrent` sets how many requests it receives at once.
- **Imported models.** The server takes no model argument; each request names a model
  ID in its own store (`litert-lm import`, `litert-lm list`). With
  `[models].auto_import` (the default), a model file loaded from anywhere is imported
  there (`litert-lm import FILE ID`) before the server starts, under its file name
  without suffix, and the server is asked for that ID. The import is a hard link, so it
  takes no extra disk space on the same drive. If the store already holds a
  *different* model under that ID, the harness stops and says so rather than replacing
  it. Setting `[litert.server].model` (or `/model … id=X`) serves that model instead.

### Python mode
Uses `litert_lm.Engine(model).create_conversation().send_message(prompt)` and opens a
fresh conversation per call. The adapter in `backends/litert.py` is defensive about API
differences between versions. If your installed version differs, adjust
`_open_engine` and `_extract_text`.

### Command-line mode (opt-in, last resort)
Set `mode = "cli"`. The default command is `litert-lm run {model} --prompt {prompt}
--max-num-tokens {context_tokens} --cache disk`. Every call starts a process that
reloads the model, so it is slow and does not stream, and the banner warns about this.
`--cache disk` keeps compiled artifacts between runs to cut that cost.

- **Prompt delivery:** `litert-lm run` accepts the prompt only through `--prompt`, so
  it is a command-line argument. It is visible in `ps` and limited in length;
  over-long prompts are refused. `prompt_via` can also be `stdin` or `file`
  (`{prompt_file}`, a private temp file deleted after the call) for wrapper scripts
  that support them.
- **Context and output length:** `--max-num-tokens` sets the context size (the KV
  cache). `run` has no flag that limits output length.
- **Output:** terminal colour codes are stripped, and `output_regex` keeps only the
  reply.
- **RAM:** runs are limited by free RAM measured while nothing is running. If the model
  size is unknown, only one runs at a time.
- **Testing your setup:** `smallharness check --probe` shows the command, the raw
  output, the extracted answer and the load time.

## Ollama
Set `[model].backend = "ollama"` (or `-b ollama`) to use a running Ollama server
(`[ollama].base_url`, default `http://127.0.0.1:11434`). The harness does not start
Ollama itself.

- **Models** are Ollama's own (GGUF, by name such as `qwen3:0.6b`), not the
  `.litertlm` files in `models/`. With the Ollama backend, `models`, `pull` and
  `delete` (and `/models`, `/pull`, `/delete`) act on Ollama's store. `-m NAME`,
  `/model NAME` and `[ollama].model` choose the model; empty means the first one
  Ollama lists.
- **Native API.** The harness uses `/api/chat`, not Ollama's OpenAI-compatible API,
  because only the native API takes the context size per request. It sends the window
  as `num_ctx`, the output limit as `num_predict`, and the stop words, with every
  request. Without `num_ctx` Ollama uses its small default and quietly cuts longer
  prompts.
- **Context size.** `context_tokens = "auto"` takes the maximum Ollama reports for the
  model (`/api/show`), capped at `auto_context_max`. When Ollama runs on this machine,
  the model's size is used to lower the window until it fits in free RAM; for a remote
  Ollama, local RAM is not considered.
- **RAM and instances.** Ollama owns the model's RAM, like a LiteRT-LM server you
  started yourself. `[ollama].max_concurrent` sets how many requests it gets at once;
  match it to `OLLAMA_NUM_PARALLEL`.
- **Token counts** stay an estimate. Ollama has no tokenizer endpoint, and its
  `prompt_eval_count` leaves out the cached part of the prompt, so it is not used to
  calibrate.
- **Reasoning models** (e.g. Qwen3): a leading `<think>…</think>` block is left out of
  the reply. `[ollama].think = false` asks Ollama not to reason at all, which is faster
  on small models.
- **Chat template.** Ollama applies the model's own template, so keep
  `[model].template = "plain"`.
- **Embeddings.** Without fastembed installed, research uses Ollama's own
  `/v1/embeddings` with `nomic-embed-text` (`ollama pull nomic-embed-text`); set
  `[rag.embed].model` to use another embedding model.
