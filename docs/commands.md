# Commands

## Terminal modes
Start with `smallharness` and choose a mode (or `smallharness chat|research|plan|recipe`).
In every mode: `/help` `/mode` (back to the menu) `/status` `/model` `/models` `/pull`
`/delete` `/config` `/resources` `/exit`. Type `\` at the end of a line to continue on
the next one.

| mode | plain text | commands |
|---|---|---|
| chat | a message (command lines first bring in tools; see [Chat](chat.md)) | `/reply` `/thread` `/main` `/pin` `/unpin` `/rewind` `/history` `/chats` `/open` `/new` `/title` `/forget` `/commands` |
| research | a question | `/add` `/files` `/remove` `/note` `/notes` `/scope` `/find` `/bibliography` `/attach` `/detach` `/answers` |
| planner | a goal to plan | `/show` `/plans` `/load` `/draft` `/edit` `/expand` `/run` `/tools` |
| recipe | (hint) | `/list` `/new` `/show` `/edit` `/write` `/library` |

In the terminal, red marks risky (type 1) tools and nothing else.

## From the shell
Every command takes `-m MODEL`, `-b litert|ollama|mock`, `-w WORKSPACE`, `-c CONFIG`,
`--ctx N` and `--trust`; `smallharness COMMAND --help` lists the rest.

```sh
smallharness index docs/ manual.pdf                     # add to the knowledge base
smallharness chat "what is a monad, in two lines?"      # one chat reply, then exit
smallharness ask "when does the release train leave?"   # one question, then exit
smallharness plan "add a --verbose flag" --expand 2 --run fresh
smallharness recipe intro --text                        # write a recipe as Markdown
smallharness notes "Remember the Friday release #ops"   # a note, no model loaded
smallharness web [--load] [--port N] [--no-browser] [--host HOST]   # the web interface
smallharness check [-m MODEL] [--probe]                 # RAM report; --probe tests the model and embedder
smallharness models | pull NAME | delete NAME           # see models.md
```
