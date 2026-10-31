# Tools

Plan runs are the only place the model calls tools. Code: `tools/`.

| tool | type |
|---|---|
| `safe_read_file`, `safe_list_files`, `safe_search_files` | 2: runs freely |
| `risky_run_command`, `risky_write_file` | 1: asks first |

`/tools` in the planner, or **Settings → Tools** on the web, lists the tools loaded
with their arguments.

## The risk is part of the name
`risky_*` tools are type 1 and ask for permission before every call, with no "always
allow" option. `safe_*` tools are type 2 and run without asking. A file with any other
name is rejected. The `safe_` file tools only reach paths inside the workspace. Calls
are parsed leniently, because small models often send malformed JSON; a bad call gets
back the error and the tool's usage.

## Writing your own
A tool is one `.py` file; add your own in `.smallharness/tools/` (or
`[tools].extra_dirs`). Tools in the workspace's own `.smallharness/tools/` load only if
the workspace is trusted (see [Workspace trust](configuration.md#workspace-trust)).
See `examples/tools/safe_word_count.py`:

```python
NAME = "safe_word_count"          # safe_* = type 2, runs freely
DESCRIPTION = "Count lines, words and characters in a workspace file."
PARAMS = {"path": {"type": "str", "desc": "file path"}}   # add "default" to make optional
def run(args, ctx): ...
```
