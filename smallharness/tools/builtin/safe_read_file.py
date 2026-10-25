NAME = "safe_read_file"
DESCRIPTION = "Read a text file from the workspace, with line numbers."
PARAMS = {
    "path": {"type": "str", "desc": "file path relative to the workspace"},
    "start": {"type": "int", "desc": "first line to show", "default": 1},
    "lines": {"type": "int", "desc": "how many lines to show", "default": 150},
}


def run(args, ctx):
    p = ctx.resolve(args["path"])
    if not p.is_file():
        return f"ERROR: {args['path']} is not a file"
    text = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, args["start"])
    end = min(len(text), start - 1 + max(1, args["lines"]))
    body = "\n".join(f"{i:>5}  {text[i - 1]}" for i in range(start, end + 1))
    more = f"\n[lines {start}-{end} of {len(text)}; use start={end + 1} for more]" if end < len(text) else ""
    return f"{args['path']}:\n{body}{more}"
