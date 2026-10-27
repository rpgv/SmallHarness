# Web interface

`smallharness web` starts a local web server and opens it in your browser. Code:
`web/`.

## Tabs
- **Chat**: your chats on the left, the conversation in the middle, and a thread on the
  right when you reply to a message. On each message: *reply*, *pin* (pinned ones are
  listed above the conversation), *rewind here*, and on yours *edit*. Typing `/` at the
  start of a line suggests commands; Tab completes one. See [Chat](chat.md).
- **Research**: *Ask* (answers with `[n]` links, Attach on each answer, the attached
  ones above the question box, a speed line, and files and notes with checkboxes for
  the scope) and *Bibliography* (each reference, and its excerpt).
- **Notes**: write, search, filter by `#tag`, edit and delete notes.
- **Planner**: create, draft or edit plans with their details; expand (which steps, how
  many passes, parallel); run fresh or continuous. Tick a step to mark it done, or
  untick it (or *untick all*) so the next run does it again. Delete a plan from its
  page or with the ✕ in the list.
- **Recipes**: write, save and run recipes, code or text, optionally in parallel.
  Untick a section and the next Write writes it again. Delete a recipe with the ✕ in
  the list.
- **Models**: use, pull with progress, delete.
- **Settings**, with two sub-tabs: *Settings* (status, every setting) and *Tools*
  (each tool, whether it asks first, and its arguments).

## Relics
Under the tabs, **Relics** lists the files recipes and plans wrote, newest first
(kept in `.smallharness/relics.json`). Open one to edit and save it; *Forget* takes it
off the list and leaves the file. A recipe's next Write rebuilds its whole file, so
edits made by hand there are replaced.

## Running
The model loads when first needed; `--load` loads it before the page opens. **Stop**
ends whatever the model is doing; risky tools ask in a dialog. Model work runs one
request at a time.

Plan runs, expansions, recipe runs and chat replies work in the background on the
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
