NAME = "safe_list_files"
DESCRIPTION = "List files in the workspace matching a glob pattern."
PARAMS = {
    "pattern": {"type": "str", "desc": "glob, e.g. **/*.py", "default": "*"},
    "path": {"type": "str", "desc": "directory to search from", "default": "."},
}
SKIP = {".git", ".smallharness", "node_modules", "__pycache__", ".venv", "venv"}
LIMIT = 200


def run(args, ctx):
    base = ctx.resolve(args["path"])
    out = []
    for p in sorted(ctx.glob(base, args["pattern"])):
        rel = p.relative_to(ctx.workspace)
        if any(part in SKIP for part in rel.parts):
            continue
        out.append(f"{rel}{'/' if p.is_dir() else ''}")
        if len(out) >= LIMIT:
            out.append(f"…[stopped at {LIMIT}; narrow the pattern]")
            break
    return "\n".join(out) or "no matches"
