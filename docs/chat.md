# Chat

A conversation with the model, built to keep its context small and clean. Start it
with `smallharness chat` (or `smallharness chat "a message"` for one reply), choose
*chat* from the mode menu, or open the **Chat** tab in the web interface. Code:
`chat.py`, `cli/chat_mode.py`.

## No tools unless you ask
The model gets no tool list in chat, and a tool call in its reply is shown as plain
text, never run. That keeps it from inventing calls and filling its context with their
results. A tool or another mode runs only when a message starts with a command line:

```
/read src/app.py
What does main() do?
```

Each line at the start of a message that begins with `/` is a command. The rest of the
message is your question, which the model answers with the commands' results in front
of it. A message that is only commands gets no reply, just the results, and the model
can see them in later messages.

| command | what it does |
|---|---|
| `/read PATH [START] [LINES]` | show a workspace file |
| `/ls [PATTERN] [DIR]` | list workspace files |
| `/grep TEXT [GLOB] [DIR]` | search file contents |
| `/run COMMAND` | run a shell command (asks first) |
| `/write PATH` | write the last answer's code block (or its text) to a file (asks first) |
| `/tool NAME ARGS`, `/NAME ARGS` | any tool, with JSON, `key=value` pairs, or values in order |
| `/agent TASK` | the model may use tools for this one message (risky ones ask) |
| `/ask QUESTION` | a [Research](research.md) answer from the knowledge base, with references |
| `/find WORDS` | the knowledge-base excerpts for some words, without the model |
| `/note TEXT`, `/notes [WORDS]` | save a note; find notes |
| `/plan GOAL`, `/runplan [ID]` | draft a [plan](planner.md); run it (the last one drafted here by default) |
| `/recipe NAME` | write a saved [recipe](recipes.md) into its file (asks first) |
| `/recall WORDS` | search this chat's turns that were folded into the summary |
| `/tools`, `/help` | list the tools, or the commands (shown, never sent to the model) |

With `/agent`, the whole message after the command is the task. Only the task and the
final answer stay in the conversation. The tool calls and their results are kept with
the answer so you can see them, but later messages don't carry them. Files that
`/write`, `/agent` or a tool command writes are listed under [Relics](web.md#relics).

## Pinning (CAG)
Pin a message (`/pin N`, or *pin* on the message) to keep it in every later prompt:
the conversation's and every thread's. A pinned message stays in the context even
after its turn has been folded into the summary. Unpin it when it no longer matters.
Pins share the `[context].retrieve_budget` part of the window (20% by default).

## Threads
Reply to a message (`/reply N TEXT`, or *reply* on the message) to start a thread on
it. A reply in a thread sees only:
- the message the thread is on, and the question that message answered,
- the thread's own replies,
- the pins.

The rest of the conversation stays out, so a side question gets a small, focused
context, and the thread never enters the main conversation's context. Replying to a
message inside a thread continues the same thread. In the terminal, `/thread N` enters
the thread (your messages go there) and `/main` goes back.

## Rewinding
Rewind to a message (`/rewind N`, or *rewind here* on the message) to clear everything
after it: the messages, the threads on them, their pins, and any summary folded since.
The next reply starts from that point, as if the rest had never been said. Inside a
thread, rewinding clears only the thread's replies after that message. *Edit* on a
message of yours clears it and everything after it, and puts its text back in the box.

## The context
Each reply's context is built by the context manager, the same one continuous plan
runs use:

1. a short system prompt (in a thread, a line naming the message it is on),
2. the rolling summary of earlier turns, once there is one,
3. the pinned messages that aren't already in the window,
4. the recent turns.

When the window passes `[context].refresh_threshold` (80% by default), the older
turns are folded into the summary, keeping the last `keep_recent_turns`. The folded
turns go into the chat's archive, where `/recall` finds them. Each thread folds on its
own. The web page shows how full the last reply's context was, and the summary above
the messages.

## Where chats are kept
Each chat is `.smallharness/chats/<id>.json`, with its archive beside it
(`<id>.sqlite`). `/chats` lists them, `/open ID` opens one, `/new` starts another,
`/title TEXT` renames the open one and `/forget ID` deletes one. In the web interface,
the list is on the left. The terminal starts a new chat each time, and a chat is saved
once it has a message.
