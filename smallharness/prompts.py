"""Prompt text and chat templates. Every system prompt is a line or two: small models
follow short, explicit structure better than long instructions."""
from __future__ import annotations

# ------------------------------------------------------------------ shared
RULES = "Be brief. Use only the facts given; if they are not enough, say so."

SUMMARIZE = """Summarize the conversation below in at most {bullets} bullets: facts, decisions,
files touched, open tasks.

PREVIOUS SUMMARY:
{previous}

CONVERSATION:
{transcript}

SUMMARY:"""

# ---------------------------------------------------------------- research
RESEARCH_SYSTEM = ("Answer only from the excerpts. After each claim put the number of the excerpt it "
                   "comes from, like [3]. If the excerpts do not answer the question, say so.")

RESEARCH = """{attached}EXCERPTS:
{excerpts}

QUESTION: {question}"""

ATTACHED = """ATTACHED (earlier answers you were asked to keep in mind):
{answers}

"""

# ----------------------------------------------------------------- planner
PLAN_SYSTEM = "You write short, concrete plans for code changes."

PLAN = """PLAN FORMAT: one task per line, at most {max_tasks}:
<number>. [<tool or none>] <short imperative task> (after: <numbers it needs>)
Leave out "(after: …)" when a task needs no earlier task. Example:
1. [safe_search_files] Find where the config is loaded
2. [safe_read_file] Read the config loader (after: 1)

TOOLS:
{tools}

GOAL: {goal}

PLAN:"""

EXPAND_SYSTEM = "You make one step of a plan more detailed. Plain text, no tool calls."

EXPAND = """EXPAND STEP {num} of the plan for: {goal}

STEP: {step}
{current}
Write the details of this step in at most {lines} lines: what to read or change, with which
tool and arguments, and how to tell it is done. Keep what is already right; add depth."""

RUN_SYSTEM = "You carry out one task of a plan in a code workspace, using the tools when needed."

TASK = """PLAN:
{plan}

{deps}CURRENT TASK {num}: {task}
{tool_hint}
{details}Do only this task. To use a tool, reply with only the call and wait for its result; do not write DONE in the same reply.
A search that finds nothing is still a result: reply DONE and say what was not found.
Finish with one line: DONE: <result>  or  FAILED: <reason>"""

# ------------------------------------------------------------------ recipe
RECIPE_SYSTEM = {
    "code": "You turn one section of a recipe into {language} code. Write only code.",
    "text": "You turn one section of a recipe into Markdown prose. Write only the text.",
}

LIBRARY = """LIBRARY (code already written). If one does exactly what this section asks, reply with
only: REUSE <id>

{snippets}

"""

RECIPE = {
    "code": """RECIPE SECTION {num} of {total} for {target}. Other sections are written separately.

SECTION {num}:
{section}

{library}Write the code it describes, with the imports it needs; no usage examples or tests.
Reply with only:
<tool>{{"name": "risky_write_file", "args": {{"path": "{target}", "content": "..."}}}}</tool>""",
    "text": """RECIPE SECTION {num} of {total} for {target}. Other sections are written separately.

SECTION {num}:
{section}

Write the paragraphs it describes, in Markdown; no title for the whole document.
Reply with only:
<tool>{{"name": "risky_write_file", "args": {{"path": "{target}", "content": "..."}}}}</tool>""",
}


def render(template: str, system: str, turns: list[tuple[str, str]]) -> str:
    """Render to one prompt string. `turns` roles: user | assistant | tool.

    plain - a labelled transcript. Use this with runtimes that apply their own chat
            template (the LiteRT-LM Conversation API wraps the whole prompt as one
            user message).
    gemma - Gemma turn markers, for runtimes that take raw text."""
    if template == "gemma":
        blocks: list[tuple[str, str]] = []
        for role, text in turns:
            r = "model" if role == "assistant" else "user"
            if role == "tool":
                text = "TOOL RESULT " + text
            if blocks and blocks[-1][0] == r:
                blocks[-1] = (r, blocks[-1][1] + "\n\n" + text)
            else:
                blocks.append((r, text))
        if not blocks or blocks[0][0] != "user":
            blocks.insert(0, ("user", ""))
        blocks[0] = ("user", system + "\n\n" + blocks[0][1])
        body = "".join(f"<start_of_turn>{r}\n{t.strip()}<end_of_turn>\n" for r, t in blocks)
        return body + "<start_of_turn>model\n"
    labels = {"user": "USER", "assistant": "ASSISTANT", "tool": "TOOL RESULT"}
    lines = [system.strip(), "", "CONVERSATION:"]
    for role, text in turns:
        lines.append(f"{labels[role]}: {text.strip()}")
    lines.append("ASSISTANT:")
    return "\n".join(lines)


STOP = {"plain": ["\nUSER:", "\nTOOL RESULT:"], "gemma": ["<end_of_turn>", "<start_of_turn>"]}
