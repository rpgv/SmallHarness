# Models

`engines.py` is the one place that knows LiteRT-LM from Ollama: model names, the
server an engine is reached at, the context window, and listing, downloading and
deleting models. The terminal and the web interface share it. For how each engine is
run, see [Engines](engines.md).

## The model folder
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
- **Short names.** `-m`, `/model` and `[model].path` take a built-in name, a file name,
  or a file name without its suffix, and find the file in the model folder. Paths to
  files anywhere else still work.

## Context size
With `[model].context_tokens = "auto"` (the default), the harness reads the context
size from the `.litertlm` file (`modelinfo.py`). It uses the metadata's
`max_num_tokens` field when the model sets it, and otherwise the shape of the KV-cache
tensors in the model, which fixes how many tokens it can hold. For gemma-4-E2B-it that
is 32,003 tokens. Only the file header and tensor tables are read (about 0.2 s).

- **auto** takes the model's maximum, capped at `auto_context_max` (8192 by default,
  because a long prompt can take minutes to process on a CPU). When RAM is managed, it
  is lowered until one instance fits. Set `auto_context_max = 0` for no cap.
- **A number** is used as given. If it is above the model's maximum, the maximum is
  used and a warning is shown.
- The context size is passed to the runtime: `--max-num-tokens` in command-line mode,
  and the engine in Python mode where the installed version accepts it.
  `litert-lm serve` sizes its own window, so in server mode the harness only keeps its
  prompts within the size.
- **RAM:** the KV-cache tensors also give the cache's exact size per token (9 KB for
  gemma-4-E2B-it, int8), which the RAM check uses in place of the rough estimate from
  the model size.
- **Token counts** are estimated on the safe side (about 3.5 characters per token).
  When the server reports the real prompt size (the `usage` field of OpenAI-style
  replies), the estimate is corrected for the model as the session goes on. `/status`
  shows which method is in use.
- `check`, `/resources` and `/model` show the context size, how it was chosen, and what
  the file says. Without a readable `.litertlm` file (an imported model ID, or a
  `.task` file), the harness uses 4096 and says so.

## RAM and instances
The harness reads total and available RAM (from psutil, `/proc/meminfo` with cgroup
limits, `vm_stat`, or Win32) and estimates the RAM one instance needs: the model file
size × `overhead_factor`, plus the KV cache for the context size (exact from the model
file when it can be read; estimated otherwise). It refuses to start a model that does
not fit. Code: `resources.py`, `pool.py`.

- The planner (expanding, `parallel` runs) starts more model
  instances when there is more than one thing to do, as many as fit. `/resources` (or
  Settings on the web) shows how many fit.
- To turn this off, set `[resources].manage_ram = false`. The harness then runs exactly
  one instance and never refuses to load. `/resources` still shows the estimate, with
  a warning if the model looks too big.
