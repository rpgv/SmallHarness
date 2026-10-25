NAME = "risky_write_file"
DESCRIPTION = "Create, write or edit a project file in the workspace. Always asks the user first."
PARAMS = {
    "path": {"type": "str", "desc": "file path relative to the workspace"},
    "content": {"type": "str", "desc": "new full content, or the replacement text when 'find' is set"},
    "find": {"type": "str", "desc": "exact text to replace once; empty writes the whole file", "default": ""},
}


def run(args, ctx):
    p = ctx.resolve(args["path"])
    if args["find"]:
        if not p.is_file():
            return f"ERROR: {args['path']} does not exist"
        text = p.read_text(encoding="utf-8")
        count = text.count(args["find"])
        if count != 1:
            return f"ERROR: 'find' text occurs {count} times; it must occur exactly once"
        p.write_text(text.replace(args["find"], args["content"], 1), encoding="utf-8")
        return f"edited {args['path']}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    return f"wrote {args['path']} ({len(args['content'])} chars)"
