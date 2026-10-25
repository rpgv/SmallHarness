"""Example drop-in tool. Copy into <workspace>/.smallharness/tools/ and it loads
on the next start. The `safe_` prefix makes it a type 2 tool (no permission
prompt); name it `risky_...` and it will ask before every call."""

NAME = "safe_word_count"
DESCRIPTION = "Count lines, words and characters in a workspace file."
PARAMS = {
    "path": {"type": "str", "desc": "file path relative to the workspace"},
}


def run(args, ctx):
    text = ctx.resolve(args["path"]).read_text(encoding="utf-8", errors="replace")
    return f"{len(text.splitlines())} lines, {len(text.split())} words, {len(text)} chars"
