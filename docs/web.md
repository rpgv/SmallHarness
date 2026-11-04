# Web interface

`smallharness web` starts a local web server and opens it in your browser. Code:
`web/`.

## Tabs
- **Chat**: your chats on the left, the conversation in the middle, and a thread on the
  right when you reply to a message. On each message: *reply*, *pin* (pinned ones are
  listed above the conversation), *rewind here*, and on yours *edit*. Typing `/` at the
  start of a line suggests commands; Tab completes one. See [Chat](chat.md).
- **Research**: *Ask* (answers with `[n]` links, *Pin* on each answer, the pinned
  ones above the thread, a speed line, and files and notes with checkboxes for the
  scope), *Gemini* (ask a larger model; pin or save its answers), *Bibliography* (each
  reference, and its excerpt) and *PubMed* (search open-access articles, add them by DOI).
- **Notes**: write, search, filter by `#tag`, edit and delete notes.
- **Code**: create, draft or edit plans, a row per step (a tool dropdown, then what
  to do) with its details and checks, or as text; expand (which steps, how many passes,
  parallel, whether to propose checks); run fresh or continuous, with the
  [persistence](code.md) gauge. Tick a step to mark it done, or
  untick it (or *untick all*) so the next run does it again. Delete a plan from its
  page or with the ✕ in the list.
- **Models**: use, pull with progress, delete; the *Speech* card chooses and pulls
  [speech](speech.md) engines and models (🎤 dictation and *speak* in chat).
- **Settings**, with two sub-tabs: *Settings* (status; *Context*, the keep-context
  switches for chats, relic chats and research; API keys; every setting) and *Tools*
  (each tool, whether it asks first, and its arguments).

## Relics
Under the tabs, **Relics** lists the files plans wrote (and chat exports), newest first
(kept in `.smallharness/relics.json`). Open one to edit and save it; *Forget* takes it
off the list and leaves the file.

- **Highlighting.** The editor numbers the lines and colours comments, strings,
  numbers, keywords and called names, by file type (Python, JavaScript/TypeScript,
  shell, C/C++, Go, Rust, Ruby, Java, Lua, SQL, HTML/XML, CSS, Markdown, JSON, TOML,
  YAML), or from a `#!` line. Tab indents; Ctrl+S saves.
- **Asking.** The *Ask* box talks with the model about the relic, in the relic's own
  chat (also listed in the Chat tab, titled `relic: PATH`). Each message carries the
  lines selected in the editor, or the whole file if nothing is selected, as they are
  in the editor, unsaved edits too. To send several parts of a large file and nothing
  else, select one and press *+ section*, then the next (the selection counts too);
  the sections are listed under the box (× drops one, a click selects it again) and
  stay for the next message. Each code block in an answer gets an *Apply* button that
  replaces those same lines (or the whole file) in the editor: with several sections, a
  menu beside it picks the section, guessed from a `Lines a-b:` label before the block,
  else from its place. Later sections move with the lines. Nothing is saved until you
  save, and Ctrl+Z undoes it. A relic chat keeps no context by default, so each reply
  sees only the message it answers and old copies of the file do not fill the prompt;
  Settings → *Context* turns that on (`[relics].keep_context`).
- **Running.** The *Run* bar below the editor is filled in with the command that
  probably runs the file: its `#!` line (or `./FILE` if it is executable), else its
  type (`python3` — the workspace's `.venv` if there is one —, `bash`, `node`, `go
  run`, compile-and-run for C, C++ and Rust, …). Edit it as you like, or type any
  command. It runs in the workspace when you press Run, with no further question,
  since it is your own command; unsaved edits are saved first. Output streams in
  below with the exit code at the end. Stop, leaving the page, or
  `[relics].run_timeout` (300 s) ends it and everything it started.

## Running
The model loads when first needed; `--load` loads it before the page opens. **Stop**
ends whatever the model is doing; risky tools ask in a dialog. Model work runs one
request at a time.

Plan runs, expansions and chat replies work in the background on the
server. Reloading the page, closing it, or a phone putting the browser to sleep does
not stop them: the page picks the run up again, log and all, and a page opened later
(or on another device) joins it too. Only **Stop** ends a run early.

## Security
- **No key to type.** It listens on `127.0.0.1` only. Requests that name another host
  are refused (DNS rebinding); requests from another site's page are refused (the
  Origin check); and every request with a body must be JSON (an upload, binary), which
  other sites cannot send without the browser asking first.
- `--host` makes it reachable from other machines: then anyone on that network can
  use it.
- Standard library only: no build step, nothing loaded from the internet (the Roboto
  Mono font, Apache 2.0, ships with it).
