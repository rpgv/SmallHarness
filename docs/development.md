# Development

```sh
pip install -e ".[all]"                 # or none of the extras: the core is standard library only
python -m unittest discover -s tests    # runs against the mock backend: no model needed
```

## Code layout
| path | what |
|---|---|
| `smallharness/cli/` | the terminal: modes, commands, one-off shell use |
| `smallharness/web/` | the web interface: server, JSON API (`routes.py`), page (`static/`) |
| `agent.py` | the harness: model calls, context, tool calls and permissions |
| `chat.py` | chat mode: commands, pins, threads, rewinding, folding |
| `research.py`, `context/` | research sessions, the knowledge base index and search, embeddings |
| `notes.py`, `notemode.py` | notes, and note mode |
| `plans.py`, `planning.py` | plans as data; creating, expanding and running them |
| `recipe.py`, `snippets.py` | recipes, and the code library |
| `relics.py` | the files recipes and plans wrote |
| `engines.py`, `models.py`, `modelinfo.py` | LiteRT-LM and Ollama: models, downloads, context size |
| `backends/` | talking to a model: LiteRT-LM server, Python and CLI, Ollama, mock |
| `resources.py`, `pool.py` | RAM checks and model instances |
| `config.py`, `trust.py`, `state.py` | settings, workspace trust, the `.smallharness/` folder |
| `tools/` | the tool registry and built-in tools |
