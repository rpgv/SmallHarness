# Commands

## Terminal modes
Start with `smallharness` and choose a mode (or `smallharness chat|research|code`).
In every mode: `/help` `/mode` (back to the menu) `/status` `/model` `/models` `/pull`
`/delete` `/config` `/resources` `/exit`. Type `\` at the end of a line to continue on
the next one.

| mode | plain text | commands |
|---|---|---|
| chat | a message (command lines first bring in tools; see [Chat](chat.md)) | `/reply` `/thread` `/main` `/pin` `/unpin` `/rewind` `/history` `/chats` `/open` `/new` `/title` `/forget` `/commands` |
| research | a question | `/add` `/files` `/remove` `/note` `/notes` `/scope` `/find` `/bibliography` `/attach` `/detach` `/answers` `/gemini` `/save` `/pubmed` `/doi` |
| code | a goal to plan | `/show` `/plans` `/load` `/draft` `/edit` `/expand` `/persist` `/permissions` `/run` `/tools` |

In the terminal, red marks risky (type 1) tools and nothing else.

## From the shell
Every command takes `-m MODEL`, `-b litert|ollama|mock`, `-w WORKSPACE`, `-c CONFIG`,
`--ctx N` and `--trust`; `smallharness COMMAND --help` lists the rest.

```sh
smallharness index docs/ manual.pdf                     # add to the knowledge base
smallharness chat "what is a monad, in two lines?"      # one chat reply, then exit
smallharness ask "when does the release train leave?"   # one question, then exit
smallharness code "add a --verbose flag" --expand 2 --checks --persist 3 --run fresh
smallharness notes "Remember the Friday release #ops"   # a note, no model loaded
smallharness web [--load] [--port N] [--no-browser] [--host HOST]   # the web interface
smallharness stt recording.wav | tts "TEXT" [-o F] [--play] | speech   # see speech.md
smallharness key [gemini|ncbi] [--remove]               # API keys for Gemini and PubMed (asked, not echoed)
smallharness check [-m MODEL] [--probe]                 # RAM report; --probe tests the model and embedder
smallharness models | pull NAME | delete NAME           # see models.md
```
