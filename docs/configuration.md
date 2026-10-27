# Configuration

## Config files
Copy `smallharness.example.toml` to `smallharness.toml` in your project to set
defaults; every key is optional, and the example lists each one with its default. Your
own defaults for every project can go in `~/.config/smallharness/config.toml`, or in a
file passed with `--config`. Command-line flags such as `-m` take precedence over both.

## Changing the model and settings while running
- **`/model NAME`** switches to another model: a name from `/models`, or a path to a
  model file. `/model` alone shows the current one. Options: `ctx=N` or `ctx=auto`,
  `out=N` (output tokens), `template=plain|gemma`, `name=X`, `mb=N` (model size for the
  RAM estimate). In server mode the first word is the model ID the server serves
  (`/model gemma-4-E4B-it`), and `path=FILE` sets the file used for the RAM estimate.
- **`/config set KEY=VALUE`** changes a setting for this run only, e.g.
  `/config set research.k=8`. Values use TOML syntax (`true`, `4096`, `["a", "b"]`).
  **`/config reload`** re-reads `smallharness.toml`; command-line flags still take
  precedence. `/config show [SECTION]` lists the settings. On the web, the Settings tab
  does the same.
- Changing the model file, `context_tokens` or anything under `[litert]` restarts the
  model, after the RAM check. If the new model fails to start, the previous settings
  and model are restored.

## Workspace trust
A project can bring code that SmallHarness would run: tool files in
`.smallharness/tools/`, and commands or tool folders in its `smallharness.toml`
(`[litert.server].command`, `[litert.cli].command`, the `[rag.ocr]` commands,
`[tools].extra_dirs`). It can also name servers that would receive your data: where
downloads (and your Hugging Face token) go (`[models].hf_endpoint`), and where prompts
and indexed files are sent (`[litert.server].base_url`, `[ollama].base_url`,
`[rag.embed].base_url`). These are used only once you trust the workspace, so opening
someone else's repository does not run its code or send your data elsewhere.

- In an interactive session the harness lists that code and asks. Your answer is
  stored in `~/.config/smallharness/trusted.json` with a fingerprint of the code, so
  any change (a new tool file, an edited command, e.g. after a `git pull`) asks again.
- Without trust, the tool files are not imported and those settings fall back to your
  own config or the defaults. Everything else in the file still applies.
- `--trust` trusts the workspace for one run without saving, e.g. for `ask` in scripts.
- Your own config (`~/.config/smallharness/config.toml`, or a file passed with
  `--config`) is always trusted.

## Where state is kept
Everything the harness keeps for a workspace is in `<project>/.smallharness/`:

| path | what |
|---|---|
| `rag/` | the knowledge base index |
| `chats/` | chats, each with its archive of folded turns |
| `notes/` | notes, one Markdown file each |
| `research/` | each research session's bibliography (temporary) |
| `library/` | files uploaded from the web interface |
| `plans/` | saved plans |
| `recipes/` | saved recipes, and each one's written sections |
| `snippets/` | the code library, one folder per language |
| `relics.json` | the files recipes and plans wrote |
| `tools/` | the workspace's own tools (loaded only if trusted) |
| `sessions/`, `history` | terminal sessions and input history |
